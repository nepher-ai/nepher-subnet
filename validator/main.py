"""
Validator main orchestrator (multi-tournament).

The central coordinator that manages the validator lifecycle across any number
of concurrently-active tournaments. It runs two independent loops:

- **Weight loop** (gpu + cpu): periodically fetches all active tournaments and
  sets ONE combined on-chain weight vector — a fixed 1% to each non-reward
  tournament's preliminary leader, the remainder to the single reward-period
  winner (or burn on UID 0).
- **Evaluation loop** (gpu only): builds a cross-tournament queue of pending
  agents (eval-period tournaments first, oldest submission first) and evaluates
  them, with lazy per-tournament setup.
"""

import asyncio
from pathlib import Path
from typing import Optional

from nepher_core.api import TournamentAPI, Tournament
from nepher_core.config import ValidatorConfig
from nepher_core.config.loader import load_config
from nepher_core.wallet import load_wallet, get_hotkey
from nepher_core.utils.logging import get_logger

from validator.state import (
    ValidatorStateManager,
    get_current_period,
)
from validator.setup import SetupManager
from validator.evaluation import EvaluationOrchestrator
from validator.evaluation.orchestrator import EVALUATION_PERIODS
from validator.reward import WeightSetter

logger = get_logger(__name__)


class ValidatorOrchestrator:
    """
    Main validator orchestrator (multi-tournament aware).

    Supports two run modes controlled by ``config.mode``:
    - **gpu** (default): full behaviour — evaluation + weight loops.
    - **cpu**: lightweight — weight loop only (no setup/evaluation).
    """

    # Cadence for combined weight setting (and idle UID-0 burns).
    BURN_INTERVAL = 1800  # 30 minutes
    # Backwards-compatible alias used in logs/tests.
    WEIGHT_INTERVAL = 1800

    def __init__(
        self,
        config: ValidatorConfig,
        config_path: Path,
        mode_override: Optional[str] = None,
    ):
        """
        Initialize validator orchestrator.

        Args:
            config: Validator configuration
            config_path: Path to the user config file (for hot-reloading)
            mode_override: CLI mode override, preserved across reloads
        """
        self.config = config
        self._config_path = config_path
        self._mode_override = mode_override
        self.mode = config.mode  # "gpu" or "cpu"

        # Load wallet and get hotkey
        wallet = load_wallet(
            name=config.wallet.name,
            hotkey=config.wallet.hotkey,
            path=config.wallet.path,
        )
        self.validator_hotkey = get_hotkey(wallet)

        self.api = TournamentAPI(
            api_key=config.api_key,
            base_url=config.api_url,
            wallet=wallet,
        )

        # State manager (per-tournament setup + period tracking)
        self.state = ValidatorStateManager()

        # Phase handlers (initialized lazily)
        self._evaluation_orchestrator: Optional[EvaluationOrchestrator] = None
        self._weight_setter: Optional[WeightSetter] = None

    def _reload_config(self) -> None:
        """
        Re-read config files from disk so that changes to common_config.yaml
        (lab_version, sim_version, eval_repo_url, etc.) are picked up before a
        tournament's setup runs.
        """
        logger.info("Reloading configuration from disk...")
        try:
            new_config = load_config(self._config_path, ValidatorConfig)
            if self._mode_override is not None:
                new_config.mode = self._mode_override

            old_isaac = self.config.isaac
            old_paths = self.config.paths
            new_isaac = new_config.isaac
            new_paths = new_config.paths

            changes: list[str] = []
            if old_isaac.lab_version != new_isaac.lab_version:
                changes.append(f"lab_version: {old_isaac.lab_version} -> {new_isaac.lab_version}")
            if old_isaac.sim_version != new_isaac.sim_version:
                changes.append(f"sim_version: {old_isaac.sim_version} -> {new_isaac.sim_version}")
            if old_paths.eval_repo_url != new_paths.eval_repo_url:
                changes.append(f"eval_repo_url: {old_paths.eval_repo_url} -> {new_paths.eval_repo_url}")

            if changes:
                for c in changes:
                    logger.info(f"  Config changed — {c}")
            else:
                logger.info("  No config changes detected")

            self.config = new_config
            self.mode = new_config.mode
        except Exception as e:
            logger.error(f"Failed to reload config — continuing with previous config: {e}")

    async def run(self) -> None:
        """Run the main validator loops."""
        logger.info("=" * 60)
        logger.info("Nepher Validator Starting (multi-tournament)")
        logger.info(f"Mode: {self.mode.upper()}")
        logger.info(f"Validator Hotkey: {self.validator_hotkey}")
        logger.info(f"Network: {self.config.subnet.network}")
        logger.info(f"Subnet UID: {self.config.subnet.subnet_uid}")
        logger.info("=" * 60)

        try:
            await self._main_loop()
        finally:
            await self.api.close()

    async def _main_loop(self) -> None:
        """Spawn the concurrent weight + evaluation loops."""
        logger.info(
            "Entering main loop",
            api_url=self.config.api_url,
            mode=self.mode,
        )

        tasks = [asyncio.create_task(self._weight_loop(), name="weight-loop")]
        if self.mode == "gpu":
            tasks.append(
                asyncio.create_task(self._evaluation_loop(), name="evaluation-loop")
            )

        await asyncio.gather(*tasks)

    # -- Active tournament fetch ---------------------------------------------

    async def _get_active_tournaments(self) -> list[Tournament]:
        """Fetch all active tournaments (multi-tournament aware)."""
        return await self.api.get_active_tournaments(subnet=True)

    # -- Weight loop (gpu + cpu) ---------------------------------------------

    async def _weight_loop(self) -> None:
        """Periodically set the combined weight vector for all active tournaments."""
        logger.info(f"Starting weight loop (every {self.BURN_INTERVAL}s)")
        if self._weight_setter is None:
            self._weight_setter = WeightSetter(self.config, self.api)

        while True:
            try:
                tournaments = await self._get_active_tournaments()
                self.state.prune({str(t.id) for t in tournaments})

                if tournaments:
                    logger.info(
                        f"[weights] {len(tournaments)} active tournament(s); "
                        "computing combined distribution"
                    )
                else:
                    logger.info("[weights] no active tournaments — burning on UID 0")

                await self._weight_setter.set_combined_weights(tournaments)
            except Exception as e:
                logger.error(f"Weight loop error: {e}", exc_info=True)

            await asyncio.sleep(self.BURN_INTERVAL)

    # -- Evaluation loop (gpu only) ------------------------------------------

    async def _evaluation_loop(self) -> None:
        """Continuously schedule evaluation across active tournaments."""
        logger.info("Starting evaluation loop")
        if self._evaluation_orchestrator is None:
            self._evaluation_orchestrator = EvaluationOrchestrator(
                config=self.config,
                api=self.api,
                validator_hotkey=self.validator_hotkey,
            )

        while True:
            try:
                tournaments = await self._get_active_tournaments()
                self.state.prune({str(t.id) for t in tournaments})

                # Record period transitions (resets setup on quiet-zone ->
                # private evaluation so the private config is re-downloaded).
                for t in tournaments:
                    self.state.update_period(str(t.id), get_current_period(t))

                await self._evaluation_orchestrator.run_evaluation_cycle(
                    tournaments,
                    is_evaluable_fn=self._is_evaluable,
                    prepare_fn=self._prepare_tournament,
                )
            except Exception as e:
                logger.error(f"Evaluation loop error: {e}", exc_info=True)

            await asyncio.sleep(self._evaluation_orchestrator.POLL_INTERVAL)

    async def _is_evaluable(self, tournament_id: str) -> bool:
        """Re-check that a tournament is still in an evaluation period."""
        try:
            tournaments = await self._get_active_tournaments()
        except Exception as e:
            logger.warning(f"Evaluable re-check fetch failed: {e}")
            return False
        for t in tournaments:
            if str(t.id) == tournament_id:
                return get_current_period(t) in EVALUATION_PERIODS
        return False

    async def _prepare_tournament(self, tournament: Tournament) -> bool:
        """Ensure per-tournament setup is complete (lazy, cached).

        Returns True when setup is ready, False on failure (the caller skips
        that tournament's agents for this cycle).
        """
        tid = str(tournament.id)
        if self.state.is_setup_complete(tid):
            return True

        try:
            # Reload config so common_config changes (eval_repo_url, versions)
            # take effect before cloning/installing the eval repo.
            self._reload_config()
            setup_manager = SetupManager(self.config, self.api)
            await setup_manager.run_setup(tid)
            self.state.mark_setup_complete(tid)
            return True
        except Exception as e:
            logger.error(f"[{tid}] setup failed: {e}")
            return False


async def run_validator(config_path: Path, mode: Optional[str] = None) -> None:
    """
    Run the validator with the given configuration.

    Args:
        config_path: Path to validator configuration file
        mode: Optional run-mode override ("cpu" or "gpu").
              When provided, takes precedence over the config file value.
    """
    # Load configuration
    config = load_config(config_path, ValidatorConfig)

    # CLI flag overrides config file value
    if mode is not None:
        config.mode = mode

    # Create and run orchestrator
    orchestrator = ValidatorOrchestrator(config, config_path, mode_override=mode)
    await orchestrator.run()
