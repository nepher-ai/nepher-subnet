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
    
    # Before contest starts
    if current_time < tournament.contest_start_time:
        return TournamentPeriod.NO_TOURNAMENT
    
    # Contest period (before submit window)
    if current_time < tournament.submit_window_start_time:
        return TournamentPeriod.CONTEST
    
    # Submit window (between submit_window_start and contest_end)
    if current_time < tournament.contest_end_time:
        return TournamentPeriod.SUBMIT_WINDOW
    
    # Evaluation period
    if current_time < tournament.evaluation_end_time:
        return TournamentPeriod.EVALUATION
    
    # Review period (between evaluation_end and reward_start)
    if tournament.reward_start_time and current_time < tournament.reward_start_time:
        return TournamentPeriod.REVIEW
    
    # Reward period
    if current_time < tournament.reward_end_time:
        return TournamentPeriod.REWARD
    
    # Completed
    return TournamentPeriod.COMPLETED


class ValidatorStateManager:
    """
    Manages validator state across tournament phases.
    
    Tracks:
    - Setup completion
    - Current tournament ID
    - Evaluation statistics
    """

    def __init__(self):
        """Initialize state manager."""
        self._setup_complete = False
        self._current_tournament_id: Optional[str] = None
        self._last_period: Optional[TournamentPeriod] = None

    @property
    def is_setup_complete(self) -> bool:
        """Check if setup has been completed for current tournament."""
        return self._setup_complete

    @property
    def current_tournament_id(self) -> Optional[str]:
        """Get current tournament ID."""
        return self._current_tournament_id

    def mark_setup_complete(self, tournament_id: str) -> None:
        """
        Mark setup as complete for a tournament.
        
        Args:
            tournament_id: Tournament ID setup was completed for
        """
        self._setup_complete = True
        self._current_tournament_id = tournament_id
        logger.info(f"Setup marked complete for tournament: {tournament_id}")

    def check_tournament_change(self, tournament_id: str) -> bool:
        """
        Check if tournament has changed (requires reset).
        
        Args:
            tournament_id: New tournament ID
            
        Returns:
            True if tournament changed
        """
        if self._current_tournament_id is None:
            return False
        return self._current_tournament_id != tournament_id

    def on_period_change(
        self,
        old_period: TournamentPeriod,
        new_period: TournamentPeriod,
    ) -> None:
        """
        Handle period transition.
        
        Args:
            old_period: Previous period
            new_period: New period
        """
        if old_period != new_period:
            logger.info(f"Period transition: {old_period.value} → {new_period.value}")
            self._last_period = new_period

    def reset(self) -> None:
        """Reset all state."""
        logger.info("Resetting validator state")
        self._setup_complete = False
        self._current_tournament_id = None
        self._last_period = None

