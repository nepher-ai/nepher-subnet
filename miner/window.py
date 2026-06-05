"""Pure helpers for reasoning about tournament submission windows.

Kept free of heavy imports (no bittensor/wallet) so miner targeting logic is
unit-testable in isolation.
"""

import time
from typing import Optional

from nepher_core.api.models import Tournament


def is_submittable(tournament: Tournament, now: Optional[int] = None) -> bool:
    """True if a tournament currently accepts agent submissions.

    Submissions are accepted during the contest/submit window:
    ``contest_start_time <= now < contest_end_time``.
    """
    if now is None:
        now = int(time.time())
    if tournament.contest_start_time is None or tournament.contest_end_time is None:
        return False
    return tournament.contest_start_time <= now < tournament.contest_end_time


def describe_stage(tournament: Tournament, now: Optional[int] = None) -> str:
    """Human-readable current stage for a tournament (for CLI listings)."""
    if now is None:
        now = int(time.time())
    cs = tournament.contest_start_time
    ce = tournament.contest_end_time
    es = tournament.evaluation_start_time
    ee = tournament.evaluation_end_time
    rs = tournament.reward_start_time
    re_ = tournament.reward_end_time
    if cs is None or now < cs:
        return "upcoming"
    if re_ is not None and now >= re_:
        return "completed"
    if rs is not None and now >= rs:
        return "reward"
    if ee is not None and now >= ee:
        return "review"
    if es is not None and now >= es:
        return "evaluation"
    if ce is not None and now >= ce:
        return "evaluation"
    sw = tournament.submit_window_start_time
    if sw is not None and now >= sw:
        return "submit"
    return "contest"
