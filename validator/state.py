"""
Tournament state and period detection.

Handles:
- Tournament period enumeration
- Period detection logic
- State management for validator
"""

import time
from enum import Enum
from typing import Optional

from nepher_core.api.models import Tournament
from nepher_core.utils.logging import get_logger

logger = get_logger(__name__)


class TournamentPeriod(Enum):
    """Tournament period states."""
    
    NO_TOURNAMENT = "no_tournament"
    CONTEST = "contest"
    PUBLIC_EVALUATION = "public_evaluation"
    QUIET_ZONE = "quiet_zone"
    SUBMIT_WINDOW = "submit_window"
    EVALUATION = "evaluation"
    REVIEW = "review"
    REWARD = "reward"
    COMPLETED = "completed"


def get_current_period(
    tournament: Optional[Tournament],
    current_time: Optional[int] = None,
) -> TournamentPeriod:
    """
    Determine current tournament period based on timestamps.
    
    Args:
        tournament: Tournament object (or None)
        current_time: Current Unix timestamp (uses time.time() if None)
        
    Returns:
        Current tournament period
    """
    if current_time is None:
        current_time = int(time.time())
    
    # No tournament or completed/cancelled
    if tournament is None:
        return TournamentPeriod.NO_TOURNAMENT
    
    if tournament.status in ["done", "cancelled"]:
        return TournamentPeriod.COMPLETED
    
    # Guard: if essential timestamps are missing, treat as no-tournament.
    # The backend may return a freshly-created tournament with null times.
    if tournament.contest_start_time is None:
        return TournamentPeriod.NO_TOURNAMENT
    
    # Before contest starts
    if current_time < tournament.contest_start_time:
        return TournamentPeriod.NO_TOURNAMENT
    
    # Public evaluation takes precedence even if submit window has started
    if (
        tournament.has_public_eval
        and tournament.public_eval_end_time
        and current_time < tournament.public_eval_end_time
    ):
        return TournamentPeriod.PUBLIC_EVALUATION
    
    # Contest sub-periods (before submit window)
    if tournament.submit_window_start_time and current_time < tournament.submit_window_start_time:
        if (
            tournament.has_public_eval
            and tournament.public_eval_end_time
            and current_time >= tournament.public_eval_end_time
        ):
            return TournamentPeriod.QUIET_ZONE
        return TournamentPeriod.CONTEST
    
    # Submit window (between submit_window_start and contest_end)
    if tournament.contest_end_time and current_time < tournament.contest_end_time:
        return TournamentPeriod.SUBMIT_WINDOW
    
    # Evaluation period
    if tournament.evaluation_end_time and current_time < tournament.evaluation_end_time:
        return TournamentPeriod.EVALUATION
    
    # Review period (between evaluation_end and reward_start)
    if tournament.reward_start_time and current_time < tournament.reward_start_time:
        return TournamentPeriod.REVIEW
    
    # Reward period
    if tournament.reward_end_time and current_time < tournament.reward_end_time:
        return TournamentPeriod.REWARD
    
    # Completed
    return TournamentPeriod.COMPLETED


class ValidatorStateManager:
    """
    Manages validator state across *multiple concurrent* tournaments.

    Tracks, keyed by tournament id:
    - Setup completion (per-tournament eval repo / task config)
    - Last observed period (for transition detection)

    Designed for the multi-tournament scheduler where several tournaments may
    be in distinct phases simultaneously, so all state is per-tournament.
    """

    def __init__(self):
        """Initialize state manager."""
        self._setup_complete: dict[str, bool] = {}
        self._last_period: dict[str, TournamentPeriod] = {}

    # -- Setup tracking -------------------------------------------------------

    def is_setup_complete(self, tournament_id: str) -> bool:
        """Check if setup has been completed for the given tournament."""
        return self._setup_complete.get(tournament_id, False)

    def mark_setup_complete(self, tournament_id: str) -> None:
        """Mark setup as complete for a tournament."""
        self._setup_complete[tournament_id] = True
        logger.info(f"Setup marked complete for tournament: {tournament_id}")

    def reset_setup(self, tournament_id: str) -> None:
        """Force a fresh setup for a tournament (e.g. public -> private phase)."""
        if self._setup_complete.get(tournament_id):
            logger.info(f"Resetting setup for tournament {tournament_id}")
        self._setup_complete[tournament_id] = False

    # -- Period transition tracking ------------------------------------------

    def get_last_period(self, tournament_id: str) -> Optional[TournamentPeriod]:
        """Return the last observed period for a tournament (or None)."""
        return self._last_period.get(tournament_id)

    def update_period(
        self,
        tournament_id: str,
        new_period: TournamentPeriod,
    ) -> Optional[TournamentPeriod]:
        """
        Record the current period for a tournament and detect transitions.

        Handles the quiet-zone -> private-evaluation handoff by forcing a
        fresh setup so the private eval config is re-downloaded.

        Args:
            tournament_id: Tournament whose period is being recorded.
            new_period: The period observed this iteration.

        Returns:
            The previous period (or None if first observation).
        """
        old_period = self._last_period.get(tournament_id)
        if old_period != new_period:
            old_name = old_period.value if old_period else "none"
            logger.info(
                f"[{tournament_id}] Period transition: {old_name} → {new_period.value}"
            )
            if new_period == TournamentPeriod.QUIET_ZONE:
                logger.info(
                    f"[{tournament_id}] Entering quiet zone — stopping evaluations, "
                    "preparing for private phase"
                )
            if new_period == TournamentPeriod.EVALUATION:
                logger.info(
                    f"[{tournament_id}] Entering private evaluation — resetting setup "
                    "to download private config"
                )
                self.reset_setup(tournament_id)
        self._last_period[tournament_id] = new_period
        return old_period

    # -- Pruning / reset ------------------------------------------------------

    def prune(self, active_ids: set[str]) -> None:
        """Drop state for tournaments that are no longer active.

        Keeps memory bounded and ensures a tournament that re-activates later
        starts from a clean slate.
        """
        for tid in list(self._setup_complete.keys()):
            if tid not in active_ids:
                self._setup_complete.pop(tid, None)
                self._last_period.pop(tid, None)
                logger.debug(f"Pruned state for inactive tournament {tid}")

    def forget(self, tournament_id: str) -> None:
        """Remove all tracked state for a single tournament."""
        self._setup_complete.pop(tournament_id, None)
        self._last_period.pop(tournament_id, None)

    def reset(self) -> None:
        """Reset all state."""
        logger.info("Resetting validator state")
        self._setup_complete.clear()
        self._last_period.clear()

