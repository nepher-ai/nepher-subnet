"""
Validator main orchestrator.

The central coordinator that manages the validator lifecycle:
- Tournament monitoring
- State machine transitions
- Phase handlers (setup, evaluation, reward)
"""

import asyncio
import time
from pathlib import Path
from typing import Optional

from nepher_core.api import TournamentAPI, Tournament
from nepher_core.config import ValidatorConfig, ConfigManager
from nepher_core.config.loader import load_config
from nepher_core.wallet import load_wallet, get_hotkey
from nepher_core.utils.logging import get_logger, setup_logging

from validator.state import (
    TournamentPeriod,
    ValidatorStateManager,
    get_current_period,
)
from validator.setup import SetupManager
from validator.evaluation import EvaluationOrchestrator
from validator.reward import WeightSetter

logger = get_logger(__name__)


class ValidatorOrchestrator:
    """
    Main validator orchestrator.
    
    Manages the complete validator lifecycle:
    1. Monitor for active tournaments
    2. Run setup during submit window
    3. Evaluate agents during evaluation period
    4. Set weights during reward period
    5. Reset and wait for next tournament
    
    Supports two run modes controlled by ``config.mode``:
    - **gpu** (default): full behaviour — setup, evaluation, reward, burn.
    - **cpu**: lightweight — reward (set-weights) and hourly burn only;
      skips setup and evaluation entirely.
    """

    # Polling intervals (seconds)
    NO_TOURNAMENT_INTERVAL = 300  # 5 minutes
    CONTEST_INTERVAL = 60  # 1 minute
    REVIEW_INTERVAL = 60  # 1 minute
    COMPLETED_INTERVAL = 300  # 5 minutes
    ERROR_INTERVAL = 60  # 1 minute
    BURN_INTERVAL = 3600  # 1 hour — cadence for UID-0 burns in CPU mode

    def __init__(self, config: ValidatorConfig):
        """
        Initialize validator orchestrator.
        
        Args:
            config: Validator configuration
        """
        self.config = config
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
        
        # State manager
        self.state = ValidatorStateManager()
        
        # Phase handlers (initialized lazily)
        self._setup_manager: Optional[SetupManager] = None
        self._evaluation_orchestrator: Optional[EvaluationOrchestrator] = None
        self._weight_setter: Optional[WeightSetter] = None
        
        # Current tournament
        self._current_tournament: Optional[Tournament] = None

    async def run(self) -> None:
        """
        Run the main validator loop.
        
        This is the primary entry point that handles all tournament phases.
        """
        logger.info("=" * 60)
        logger.info("Nepher Validator Starting")
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
        """Main validator loop."""
        logger.info(
            "Entering main loop",
            api_url=self.config.api_url,
            poll_interval=f"{self.NO_TOURNAMENT_INTERVAL}s",
        )

        iteration = 0
        while True:
            iteration += 1
            try:
                # 1. Check for active tournament
                logger.info(f"[iter {iteration}] Checking for active tournament...")
                tournament = await self.api.get_active_tournament()
                self._current_tournament = tournament
                
                if tournament is None:
                    logger.info(
                        f"No active tournament. Sleeping {self.NO_TOURNAMENT_INTERVAL}s before next check..."
                    )
                    await asyncio.sleep(self.NO_TOURNAMENT_INTERVAL)
                    continue
                
                # Check for tournament change
                if self.state.check_tournament_change(tournament.id):
                    logger.info(f"Tournament changed to: {tournament.id}")
                    self.state.reset()
                
                # 2. Determine current period
                current_time = int(time.time())
                period = get_current_period(tournament, current_time)
                logger.info(
                    f"[iter {iteration}] Tournament {tournament.id} — "
                    f"period={period.name}, status={tournament.status}"
                )
                
                # 3. Handle state transitions
                await self._handle_period(tournament, period)
                
            except Exception as e:
                logger.error(
                    f"[iter {iteration}] Main loop error: {e}",
                    exc_info=True,
                )
                await asyncio.sleep(self.ERROR_INTERVAL)

    async def _handle_period(
        self,
        tournament: Tournament,
        period: TournamentPeriod,
    ) -> None:
        """
        Handle current tournament period.
        
        Behaviour depends on ``self.mode``:
        - **gpu** (default): full lifecycle — setup, evaluation, reward, idle waits.
        - **cpu**: reward + hourly burn only; evaluation/setup are skipped.
        
        Args:
            tournament: Current tournament
            period: Current period
        """
        match period:
            # ── Shared across both modes ─────────────────────────────
            case TournamentPeriod.NO_TOURNAMENT:
                self.state.reset()
                await asyncio.sleep(self.NO_TOURNAMENT_INTERVAL)

            case TournamentPeriod.REWARD:
                await self._run_reward(tournament)

            # ── CPU-only: burn on UID 0 once per hour ────────────────
            case _ if self.mode == "cpu":
                await self._hourly_burn()

            # ── GPU-only handlers (full behaviour) ───────────────────
            case TournamentPeriod.CONTEST:
                logger.debug("Contest period - waiting...")
                await asyncio.sleep(self.CONTEST_INTERVAL)

            case TournamentPeriod.PUBLIC_EVALUATION:
                if not self.state.is_setup_complete:
                    await self._run_setup(tournament)
                await self._run_evaluation(tournament, phase="public")

            case TournamentPeriod.QUIET_ZONE:
                logger.info(
                    "Quiet zone — downloading private config, clearing public artifacts. "
                    f"Private evaluation starts at {tournament.evaluation_start_time}"
                )
                if self._setup_manager:
                    self._setup_manager.reset()
                self.state.reset()
                await asyncio.sleep(self.CONTEST_INTERVAL)
                
            case TournamentPeriod.SUBMIT_WINDOW:
                logger.debug("Submit window - waiting for evaluation period...")
                await asyncio.sleep(self.CONTEST_INTERVAL)
                    
            case TournamentPeriod.EVALUATION:
                if not self.state.is_setup_complete:
                    await self._run_setup(tournament)
                await self._run_evaluation(tournament, phase="private")
                
            case TournamentPeriod.REVIEW:
                logger.info("Review period - waiting for admin approval...")
                await asyncio.sleep(self.REVIEW_INTERVAL)
                
            case TournamentPeriod.COMPLETED:
                logger.info("Tournament completed")
                self.state.reset()
                await asyncio.sleep(self.COMPLETED_INTERVAL)

    async def _run_setup(self, tournament: Tournament) -> None:
        """Run setup phase."""
        if self._setup_manager is None:
            self._setup_manager = SetupManager(self.config, self.api)
        
        try:
            await self._setup_manager.run_setup(tournament.id)
            self.state.mark_setup_complete(tournament.id)
            
            # Load task config into validator config
            task_config_path = self.config.paths.workspace / "task_config.yaml"
            if task_config_path.exists():
                config_manager = ConfigManager()
                self.config.task_config = config_manager.load_task_config(task_config_path)
                
        except Exception as e:
            logger.error(f"Setup failed: {e}")
            raise

    async def _run_evaluation(self, tournament: Tournament, phase: str = "private") -> None:
        """Run evaluation loop for the given phase."""
        if self._evaluation_orchestrator is None:
            self._evaluation_orchestrator = EvaluationOrchestrator(
                config=self.config,
                api=self.api,
                validator_hotkey=self.validator_hotkey,
            )
        
        expected_period = (
            TournamentPeriod.PUBLIC_EVALUATION if phase == "public"
            else TournamentPeriod.EVALUATION
        )
        
        async def is_evaluation_period() -> bool:
            fresh = await self.api.get_active_tournament()
            return get_current_period(fresh) == expected_period
        
        logger.info(f"Starting {phase} evaluation loop")
        await self._evaluation_orchestrator.run_evaluation_loop(
            tournament=tournament,
            is_evaluation_period_fn=is_evaluation_period,
        )

    async def _hourly_burn(self) -> None:
        """
        Burn on UID 0 once, then sleep for ~1 hour.
        
        Used by the CPU validator during non-reward tournament periods
        to maintain chain presence without running evaluations.
        """
        if self._weight_setter is None:
            self._weight_setter = WeightSetter(self.config, self.api)
        
        await self._weight_setter.burn()
        
        logger.info(f"Burn complete. Next burn in ~{self.BURN_INTERVAL}s")
        await asyncio.sleep(self.BURN_INTERVAL)

    async def _run_reward(self, tournament: Tournament) -> None:
        """Run reward phase."""
        if self._weight_setter is None:
            self._weight_setter = WeightSetter(self.config, self.api)
        
        async def is_reward_period() -> bool:
            """Re-fetch tournament each check so schedule changes are detected."""
            fresh = await self.api.get_active_tournament()
            return get_current_period(fresh) == TournamentPeriod.REWARD
        
        await self._weight_setter.run_reward(
            tournament=tournament,
            is_reward_period_fn=is_reward_period,
        )


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
    orchestrator = ValidatorOrchestrator(config)
    await orchestrator.run()

