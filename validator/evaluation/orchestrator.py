"""
Evaluation orchestrator (multi-tournament).

Schedules evaluation of pending agents across ALL concurrently-active
tournaments:

- Tournaments currently in an evaluation period (public or private) are
  prioritized.
- Within the work set, agents are processed oldest-submission-first
  (age-based ordering).
- When no tournament in an evaluation period has pending agents, the
  scheduler falls back to age-based ordering across every active tournament.
- Each agent is guarded by a per-tournament "still evaluable?" check so work
  is skipped once a tournament leaves its evaluation window.
"""

import asyncio
from typing import Awaitable, Callable

from nepher_core.api import TournamentAPI, Tournament, Agent, QuietZoneError
from nepher_core.config import ValidatorConfig
from nepher_core.utils.logging import get_logger
from validator.evaluation.agent_evaluator import AgentEvaluator, EvaluationError
from validator.state import TournamentPeriod, get_current_period

logger = get_logger(__name__)


# Tournament periods during which agents are evaluated.
EVALUATION_PERIODS = (
    TournamentPeriod.PUBLIC_EVALUATION,
    TournamentPeriod.EVALUATION,
)


class EvaluationOrchestrator:
    """
    Orchestrates evaluation of pending agents across multiple tournaments.

    Responsibilities:
    - Determine which active tournaments are in an evaluation period.
    - Fetch their pending agents and build a single prioritized, age-ordered
      work queue.
    - Ensure per-tournament setup before evaluating an agent.
    - Evaluate each agent, handling failures and reporting gracefully.
    """

    POLL_INTERVAL = 60  # seconds between evaluation cycles when idle
    AGENT_DELAY = 5  # seconds between agent evaluations

    def __init__(
        self,
        config: ValidatorConfig,
        api: TournamentAPI,
        validator_hotkey: str,
    ):
        self.config = config
        self.api = api
        self.validator_hotkey = validator_hotkey
        self.evaluator = AgentEvaluator(config, api, validator_hotkey)

        # Statistics
        self._evaluated_count = 0
        self._failed_count = 0

    @property
    def stats(self) -> dict:
        """Get evaluation statistics."""
        return {
            "evaluated": self._evaluated_count,
            "failed": self._failed_count,
            "total": self._evaluated_count + self._failed_count,
        }

    # -- Queue construction (pure, unit-testable) -----------------------------

    @staticmethod
    def phase_for_period(period: TournamentPeriod) -> str:
        """Leaderboard/eval phase string for a period."""
        return "public" if period == TournamentPeriod.PUBLIC_EVALUATION else "private"

    @staticmethod
    def _age_key(agent: Agent) -> float:
        """Sort key for age-based ordering: oldest submission first.

        Agents without a ``created_at`` sort last (treated as newest).
        """
        ts = agent.created_at
        if ts is None:
            return float("inf")
        try:
            return ts.timestamp()
        except Exception:
            return float("inf")

    @classmethod
    def build_evaluation_queue(
        cls,
        period_by_tid: dict[str, TournamentPeriod],
        pending_by_tid: dict[str, list[Agent]],
    ) -> list[tuple[str, Agent]]:
        """
        Build the ordered ``(tournament_id, agent)`` work queue.

        Prioritizes agents from tournaments currently in an evaluation period,
        ordered oldest-first. If no such agents exist, falls back to age-based
        ordering across all supplied pending agents.
        """
        eval_tids = {
            tid
            for tid, period in period_by_tid.items()
            if period in EVALUATION_PERIODS
        }

        prioritized: list[tuple[str, Agent]] = [
            (tid, agent)
            for tid, agents in pending_by_tid.items()
            if tid in eval_tids
            for agent in agents
        ]
        if prioritized:
            prioritized.sort(key=lambda pair: cls._age_key(pair[1]))
            return prioritized

        # Fallback: order every pending agent across active tournaments by age.
        fallback: list[tuple[str, Agent]] = [
            (tid, agent)
            for tid, agents in pending_by_tid.items()
            for agent in agents
        ]
        fallback.sort(key=lambda pair: cls._age_key(pair[1]))
        return fallback

    # -- Main scheduling cycle ------------------------------------------------

    async def run_evaluation_cycle(
        self,
        tournaments: list[Tournament],
        is_evaluable_fn: Callable[[str], Awaitable[bool]],
        prepare_fn: Callable[[Tournament], Awaitable[bool]],
    ) -> int:
        """
        Run a single evaluation cycle across all active tournaments.

        Args:
            tournaments: Currently active tournaments.
            is_evaluable_fn: ``async (tournament_id) -> bool`` re-check that a
                tournament is still in an evaluation period (called per agent).
            prepare_fn: ``async (tournament) -> bool`` ensure per-tournament
                setup is complete; returns False if setup failed (skip it).

        Returns:
            Number of agents processed this cycle.
        """
        if not tournaments:
            return 0

        tournament_by_id = {str(t.id): t for t in tournaments}
        period_by_tid = {
            str(t.id): get_current_period(t) for t in tournaments
        }

        eval_tids = [
            tid for tid, period in period_by_tid.items()
            if period in EVALUATION_PERIODS
        ]

        pending_by_tid: dict[str, list[Agent]] = {}
        for tid in eval_tids:
            pending_by_tid[tid] = await self._fetch_pending(
                tid, self.phase_for_period(period_by_tid[tid])
            )

        # Fallback: if no eval-period tournament has agents, consider all actives.
        if not any(pending_by_tid.get(tid) for tid in eval_tids):
            for tid, period in period_by_tid.items():
                if tid in pending_by_tid:
                    continue
                pending_by_tid[tid] = await self._fetch_pending(
                    tid, self.phase_for_period(period)
                )

        queue = self.build_evaluation_queue(period_by_tid, pending_by_tid)
        if not queue:
            logger.info("No pending agents across active tournaments")
            return 0

        logger.info(f"Evaluation queue built: {len(queue)} agent(s)")

        processed = 0
        prepared: set[str] = set()
        failed_setup: set[str] = set()

        for tid, agent in queue:
            # Re-check the tournament is still evaluable (window may have closed).
            try:
                if not await is_evaluable_fn(tid):
                    logger.info(
                        f"[{tid}] no longer in an evaluation period — skipping agent {agent.id}"
                    )
                    continue
            except Exception as e:
                logger.warning(f"[{tid}] evaluable check failed: {e}")
                continue

            if tid in failed_setup:
                continue

            tournament = tournament_by_id.get(tid)
            if tournament is None:
                continue

            # Lazy, cached per-tournament setup.
            if tid not in prepared:
                try:
                    ok = await prepare_fn(tournament)
                except Exception as e:
                    logger.error(f"[{tid}] setup raised: {e}")
                    ok = False
                if not ok:
                    logger.error(f"[{tid}] setup failed — skipping its agents this cycle")
                    failed_setup.add(tid)
                    continue
                prepared.add(tid)

            await self._evaluate_one(tid, agent)
            processed += 1
            await asyncio.sleep(self.AGENT_DELAY)

        logger.info(f"Evaluation cycle complete. Stats: {self.stats}")
        return processed

    async def _fetch_pending(self, tournament_id: str, phase: str) -> list[Agent]:
        """Fetch pending agents for one tournament; never raises."""
        try:
            response = await self.api.get_pending_agents(
                tournament_id=tournament_id,
                validator_hotkey=self.validator_hotkey,
                phase=phase,
            )
            return list(response.agents)
        except Exception as e:
            logger.warning(f"[{tournament_id}] failed to fetch pending agents: {e}")
            return []

    async def _evaluate_one(self, tournament_id: str, agent: Agent) -> None:
        """Evaluate a single agent, handling failures + reporting."""
        try:
            await self.evaluator.evaluate(tournament_id=tournament_id, agent=agent)
            self._evaluated_count += 1

        except QuietZoneError:
            logger.info(f"[{tournament_id}] quiet zone reached during agent {agent.id}")

        except EvaluationError as e:
            logger.error(f"Evaluation failed for agent {agent.id}: {e.message}")
            self._failed_count += 1

            try:
                await self.api.submit_failed_evaluation(
                    tournament_id=tournament_id,
                    agent_id=agent.id,
                    validator_hotkey=self.validator_hotkey,
                    error_reason=e.message,
                )
            except QuietZoneError:
                pass
            except Exception as submit_error:
                logger.error(f"Failed to submit failure: {submit_error}")

            log_content = e.log_output if e.log_output else e.message
            await self.api.report_agent(
                tournament_id=tournament_id,
                agent_id=agent.id,
                validator_hotkey=self.validator_hotkey,
                log_content=log_content,
                error_type=self._classify_error(log_content),
                summary=e.message[:500],
            )

        except Exception as e:
            logger.error(f"Unexpected error evaluating agent {agent.id}: {e}")
            self._failed_count += 1

    @staticmethod
    def _classify_error(message: str) -> str:
        """Classify an error message into a report error_type.

        Keywords are intentionally specific to avoid false positives from
        Isaac Lab config dumps that contain URLs (``https://…``) and
        parameter names like ``timeout``.
        """
        msg = message.lower()
        network_kw = (
            "connectionerror", "connectionrefused", "connectionreset",
            "requests.post", "requests.get", "urllib3",
            "httpx.", "httperror", "httpstatuserror",
            "network is unreachable", "name resolution",
            "upload failed", "download failed",
        )
        if any(kw in msg for kw in network_kw):
            return "network_violation"
        if any(kw in msg for kw in ("permission denied", "iptables", "capability", "sandbox_escape")):
            return "sandbox_escape"
        if any(kw in msg for kw in ("timed out", "timedouterror", "evaluationtimeouterror")):
            return "timeout"
        if any(kw in msg for kw in ("modulenotfounderror", "no module named", "importerror")):
            return "import_error"
        return "runtime_error"

    def reset_stats(self) -> None:
        """Reset evaluation statistics."""
        self._evaluated_count = 0
        self._failed_count = 0
