"""Tests for multi-tournament validator + miner behaviour."""

import time
from datetime import datetime, timedelta, timezone

import pytest

from nepher_core.api.models import Tournament, Agent
from validator.state import (
    TournamentPeriod,
    ValidatorStateManager,
    get_current_period,
)
from validator.evaluation.orchestrator import EvaluationOrchestrator
from validator.reward.distribution import (
    compute_weight_distribution,
    LEADER_WEIGHT_FRACTION,
    DEFAULT_BURN_UID,
)


T = 1_700_000_000  # fixed reference "now"


def make_tournament(tid: str, kind: str) -> Tournament:
    """Build a Tournament whose timestamps place it in the given period at T."""
    base = dict(
        id=tid,
        status="active",
        contest_start_time=T - 1000,
        submit_window_start_time=T - 900,
        contest_end_time=T - 800,
        evaluation_start_time=T - 800,
        evaluation_end_time=T - 600,
        reward_start_time=T - 400,
        reward_end_time=T - 200,
        has_public_eval=False,
    )
    if kind == "reward":
        base.update(reward_start_time=T - 100, reward_end_time=T + 1000)
    elif kind == "evaluation":
        base.update(
            contest_end_time=T - 500,
            evaluation_start_time=T - 500,
            evaluation_end_time=T + 1000,
            reward_start_time=T + 2000,
            reward_end_time=T + 3000,
        )
    elif kind == "review":
        base.update(
            evaluation_end_time=T - 100,
            reward_start_time=T + 1000,
            reward_end_time=T + 2000,
        )
    elif kind == "contest":
        base.update(
            contest_start_time=T - 10,
            submit_window_start_time=T + 1000,
            contest_end_time=T + 2000,
            evaluation_start_time=T + 2000,
            evaluation_end_time=T + 3000,
            reward_start_time=T + 4000,
            reward_end_time=T + 5000,
        )
    else:
        raise ValueError(kind)
    return Tournament(**base)


def make_agent(agent_id: str, created_offset_sec: int) -> Agent:
    return Agent(
        id=agent_id,
        created_at=datetime.now(timezone.utc) + timedelta(seconds=created_offset_sec),
    )


# ---------------------------------------------------------------------------
# Period detection
# ---------------------------------------------------------------------------

class TestPeriodDetection:
    def test_reward(self):
        assert get_current_period(make_tournament("a", "reward"), T) == TournamentPeriod.REWARD

    def test_evaluation(self):
        assert get_current_period(make_tournament("a", "evaluation"), T) == TournamentPeriod.EVALUATION

    def test_review(self):
        assert get_current_period(make_tournament("a", "review"), T) == TournamentPeriod.REVIEW

    def test_contest(self):
        assert get_current_period(make_tournament("a", "contest"), T) == TournamentPeriod.CONTEST


# ---------------------------------------------------------------------------
# Evaluation queue ordering
# ---------------------------------------------------------------------------

class TestEvaluationQueue:
    def test_eval_period_prioritized_and_age_ordered(self):
        period_by_tid = {
            "eval1": TournamentPeriod.EVALUATION,
            "eval2": TournamentPeriod.PUBLIC_EVALUATION,
            "review1": TournamentPeriod.REVIEW,
        }
        pending_by_tid = {
            "eval1": [make_agent("a_new", 100), make_agent("a_old", 0)],
            "eval2": [make_agent("b_mid", 50)],
            "review1": [make_agent("r", -1000)],  # oldest, but not eval period
        }
        queue = EvaluationOrchestrator.build_evaluation_queue(period_by_tid, pending_by_tid)
        # review tournament agent must NOT appear (eval-period agents exist)
        ids = [a.id for _, a in queue]
        assert "r" not in ids
        # oldest-first across eval tournaments: a_old(0) < b_mid(50) < a_new(100)
        assert ids == ["a_old", "b_mid", "a_new"]

    def test_fallback_age_order_when_no_eval_pending(self):
        period_by_tid = {
            "review1": TournamentPeriod.REVIEW,
            "contest1": TournamentPeriod.CONTEST,
        }
        pending_by_tid = {
            "review1": [make_agent("r_new", 100)],
            "contest1": [make_agent("c_old", 0)],
        }
        queue = EvaluationOrchestrator.build_evaluation_queue(period_by_tid, pending_by_tid)
        ids = [a.id for _, a in queue]
        # No eval-period agents -> fallback orders everything by age (oldest first)
        assert ids == ["c_old", "r_new"]

    def test_empty(self):
        assert EvaluationOrchestrator.build_evaluation_queue({}, {}) == []


# ---------------------------------------------------------------------------
# Weight distribution
# ---------------------------------------------------------------------------

class TestWeightDistribution:
    def test_fixed_one_percent_plus_reward_winner(self):
        # two non-reward leaders (UIDs 10, 11) + reward winner (UID 20)
        dist = compute_weight_distribution([10, 11], reward_winner_uid=20)
        assert dist[10] == pytest.approx(0.01)
        assert dist[11] == pytest.approx(0.01)
        assert dist[20] == pytest.approx(0.98)
        assert sum(dist.values()) == pytest.approx(1.0)

    def test_no_reward_burns_remainder(self):
        dist = compute_weight_distribution([10], reward_winner_uid=None)
        assert dist[10] == pytest.approx(0.01)
        assert dist[DEFAULT_BURN_UID] == pytest.approx(0.99)
        assert sum(dist.values()) == pytest.approx(1.0)

    def test_leader_equals_winner_merges(self):
        # leader UID 7 also the reward winner -> 1% + 0.99 == 1.0 on one UID
        dist = compute_weight_distribution([7], reward_winner_uid=7)
        assert dist[7] == pytest.approx(1.0)
        assert sum(dist.values()) == pytest.approx(1.0)

    def test_leader_not_in_metagraph_skipped(self):
        # None leader -> its 1% rolls into the remainder for the winner
        dist = compute_weight_distribution([None], reward_winner_uid=20)
        assert dist[20] == pytest.approx(1.0)
        assert sum(dist.values()) == pytest.approx(1.0)

    def test_no_tournaments_all_burn(self):
        dist = compute_weight_distribution([], reward_winner_uid=None)
        assert dist[DEFAULT_BURN_UID] == pytest.approx(1.0)

    def test_leader_collides_with_burn(self):
        # leader is UID 0 (burn uid) and no reward winner -> sums to 1.0
        dist = compute_weight_distribution([0], reward_winner_uid=None)
        assert dist[0] == pytest.approx(1.0)
        assert sum(dist.values()) == pytest.approx(1.0)

    def test_leader_fraction_constant(self):
        assert LEADER_WEIGHT_FRACTION == pytest.approx(0.01)


# ---------------------------------------------------------------------------
# State manager
# ---------------------------------------------------------------------------

class TestStateManager:
    def test_per_tournament_setup(self):
        s = ValidatorStateManager()
        assert not s.is_setup_complete("t1")
        s.mark_setup_complete("t1")
        assert s.is_setup_complete("t1")
        assert not s.is_setup_complete("t2")

    def test_quiet_to_eval_resets_setup(self):
        s = ValidatorStateManager()
        s.mark_setup_complete("t1")
        s.update_period("t1", TournamentPeriod.QUIET_ZONE)
        s.update_period("t1", TournamentPeriod.EVALUATION)
        assert not s.is_setup_complete("t1")

    def test_prune(self):
        s = ValidatorStateManager()
        s.mark_setup_complete("t1")
        s.mark_setup_complete("t2")
        s.prune({"t2"})
        assert not s.is_setup_complete("t1")
        assert s.is_setup_complete("t2")


# ---------------------------------------------------------------------------
# Miner targeting helpers
# ---------------------------------------------------------------------------

class TestMinerHelpers:
    def test_is_submittable(self):
        from miner.window import is_submittable
        assert is_submittable(make_tournament("c", "contest"), now=T)
        # reward-period tournament is not accepting submissions
        assert not is_submittable(make_tournament("r", "reward"), now=T)

    def test_describe_stage(self):
        from miner.window import describe_stage
        assert describe_stage(make_tournament("r", "reward"), now=T) == "reward"
        assert describe_stage(make_tournament("e", "evaluation"), now=T) == "evaluation"
