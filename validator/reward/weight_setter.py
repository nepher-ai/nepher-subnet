"""
Weight setting logic for reward phase.

Handles:
- Querying winner from API
- Finding winner UID in metagraph
- Setting weights on chain
- Burning on UID 0 when no winner or weight is unallocated
- Deduplicating weight sets across CPU/GPU validators sharing a hotkey
"""

import asyncio
import hashlib
from datetime import datetime, timezone
from typing import Optional, List

import bittensor as bt
from bittensor_wallet import Wallet

from nepher_core.api import TournamentAPI, Tournament
from nepher_core.api.models import PodiumWinnerInfo
from nepher_core.config import ValidatorConfig
from nepher_core.wallet.utils import (
    load_wallet,
    get_subtensor,
    get_metagraph,
    find_uid_for_hotkey,
)
from nepher_core.utils.logging import get_logger
from validator.state import TournamentPeriod, get_current_period
from validator.reward.distribution import compute_weight_distribution, RewardRecipient

logger = get_logger(__name__)


class WeightLookupError(Exception):
    """A backend lookup needed for the weight vector failed.

    Raised instead of falling back to a burn so that a transient API failure
    leaves the existing on-chain weights untouched.
    """


def compute_weight_hash(weight_map: dict[int, float]) -> str:
    """Deterministic SHA-256 of a weight distribution."""
    canonical = ",".join(f"{uid}:{w}" for uid, w in sorted(weight_map.items()))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _as_utc(ts: datetime) -> datetime:
    """Naive timestamps from the backend are UTC; aware ones are converted."""
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


class WeightSetter:
    """
    Handles weight setting during reward phase.

    Responsibilities:
    - Query winner from tournament API
    - Find winner's UID in metagraph
    - Set all weight to winner UID
    - Burn on UID 0 if no winner or winner not found
    - Allocate a small fraction to the preliminary leader during non-reward periods
    """

    BURN_UID = 0  # UID to burn to when no winner
    LEADER_WEIGHT_FRACTION = 0.01  # 1% emission to preliminary leader
    DEDUP_WINDOW = 900  # 15 minutes — skip if identical weights committed within this window
    METAGRAPH_TIMEOUT = 120  # seconds
    SET_WEIGHTS_TIMEOUT = 180  # seconds per attempt

    def __init__(
        self,
        config: ValidatorConfig,
        api: TournamentAPI,
    ):
        """
        Initialize weight setter.

        Args:
            config: Validator configuration
            api: Tournament API client
        """
        self.config = config
        self.api = api
        self._wallet: Optional[Wallet] = None
        self._subtensor: Optional[bt.Subtensor] = None
        self._metagraph: Optional[bt.Metagraph] = None

    def _load_wallet(self) -> Wallet:
        """Load wallet for signing transactions."""
        if self._wallet is None:
            self._wallet = load_wallet(
                name=self.config.wallet.name,
                hotkey=self.config.wallet.hotkey,
                path=self.config.wallet.path,
            )
        return self._wallet

    async def _get_subtensor(self) -> bt.Subtensor:
        """Get (or lazily open) the subtensor connection off the event loop."""
        if self._subtensor is None:
            self._subtensor = await asyncio.wait_for(
                asyncio.to_thread(get_subtensor, self.config.subnet.network),
                timeout=self.METAGRAPH_TIMEOUT,
            )
        return self._subtensor

    def _reset_subtensor(self) -> None:
        """Drop a possibly-dead connection so the next call reconnects."""
        subtensor, self._subtensor = self._subtensor, None
        if subtensor is not None:
            try:
                subtensor.close()
            except Exception:
                pass

    async def _get_metagraph(self) -> bt.Metagraph:
        """Fetch a fresh metagraph without blocking the event loop."""
        try:
            subtensor = await self._get_subtensor()
            self._metagraph = await asyncio.wait_for(
                asyncio.to_thread(get_metagraph, subtensor, self.config.subnet.subnet_uid),
                timeout=self.METAGRAPH_TIMEOUT,
            )
        except Exception:
            self._reset_subtensor()
            raise
        return self._metagraph

    async def set_combined_weights(self, tournaments: List[Tournament]) -> None:
        """
        Compute and set ONE combined weight vector across all active tournaments.

        This is the single weight-setting entry point for the multi-tournament
        validator. Each non-reward tournament contributes a fixed 1% to its
        preliminary leader; the single reward-period tournament's winner
        receives all remaining weight; anything unallocated burns on UID 0.

        Args:
            tournaments: All currently-active tournaments (may be empty).

        Raises:
            WeightLookupError: If a backend lookup failed; no weights are set.
        """
        metagraph = await self._get_metagraph()

        if not tournaments:
            logger.info("No active tournaments — burning 100% on UID 0")
            await self._set_weight_distribution({self.BURN_UID: 1.0}, metagraph)
            return

        weight_map = await self.compute_distribution(tournaments, metagraph)
        await self._set_weight_distribution(weight_map, metagraph)

    async def compute_distribution(
        self,
        tournaments: List[Tournament],
        metagraph: bt.Metagraph,
    ) -> dict[int, float]:
        """
        Build the combined ``{uid: weight}`` distribution for active tournaments.

        Rules (see incentive mechanism / plan):
        - Each tournament NOT in its reward period contributes ``LEADER_WEIGHT_FRACTION``
          (1%) to its current preliminary leader, if that leader resolves to a UID.
        - Among tournaments in their reward period, prefer one with an approved
          winner for the remainder (``1 - sum(fixed allocations)``). Confirmed
          no-winner tournaments may legally overlap and are skipped for remainder.
        - If there is no reward tournament with an approved winner (or its winner
          is not in the metagraph), the remainder burns on UID 0.
        - Duplicate UIDs (leader == winner == burn) are merged by addition; the
          final map is normalized to sum to 1.0.

        Tournaments are processed in a deterministic (id-sorted) order so that
        CPU and GPU validators sharing a hotkey produce identical weight hashes.

        Raises:
            WeightLookupError: If a leader or winner lookup failed. A failed
                lookup is never treated as "no leader" / "no winner".
        """
        sorted_tournaments = sorted(tournaments, key=lambda t: str(t.id))

        leader_uids: list[Optional[int]] = []
        reward_tournaments: List[Tournament] = []

        for tournament in sorted_tournaments:
            period = get_current_period(tournament)

            if period == TournamentPeriod.REWARD:
                reward_tournaments.append(tournament)
                continue

            # Non-reward tournament: allocate a fixed 1% to its preliminary leader.
            phase = (
                "public"
                if period == TournamentPeriod.PUBLIC_EVALUATION
                else "private"
            )
            try:
                leader = await self.api.get_preliminary_leader(tournament.id, phase=phase)
            except Exception as e:
                raise WeightLookupError(
                    f"[{tournament.id}] preliminary-leader lookup failed: {e}"
                ) from e

            if not leader.leader_hotkey:
                continue
            leader_uid = find_uid_for_hotkey(metagraph, leader.leader_hotkey)
            if leader_uid is None:
                logger.info(
                    f"[{tournament.id}] leader {leader.leader_hotkey[:16]}… not in "
                    "metagraph — skipping its 1% (rolls into remainder)"
                )
                continue

            leader_uids.append(leader_uid)
            logger.info(
                f"[{tournament.id}] fixed {self.LEADER_WEIGHT_FRACTION:.0%} -> "
                f"UID {leader_uid} (leader)"
            )

        # Resolve the remainder recipients: prefer a reward tournament with an
        # approved podium. Confirmed no-winner tournaments may legally share a
        # reward window with another tournament and must not steal the remainder.
        reward_recipients: Optional[List[RewardRecipient]] = None
        if reward_tournaments:
            if len(reward_tournaments) > 1:
                ids = ", ".join(str(t.id) for t in reward_tournaments)
                logger.warning(
                    f"Multiple tournaments in reward period simultaneously ({ids}); "
                    "preferring one with an approved winner (no-winner windows may overlap)"
                )
                reward_tournaments.sort(
                    key=lambda t: (t.reward_start_time or 0, str(t.id))
                )

            chosen = None
            for candidate in reward_tournaments:
                recipients = await self._get_reward_recipients(candidate.id, metagraph)
                if recipients:
                    chosen = candidate
                    reward_recipients = recipients
                    desc = ", ".join(
                        f"UID {uid}@{share:.1%}" for uid, share in recipients
                    )
                    logger.info(
                        f"[{chosen.id}] reward podium receives the remainder: {desc}"
                    )
                    break

            if chosen is None:
                logger.info(
                    "No approved reward winner among overlapping reward "
                    "tournaments — remainder burns on UID 0"
                )

        return compute_weight_distribution(
            leader_uids,
            reward_recipients=reward_recipients,
            burn_uid=self.BURN_UID,
            leader_fraction=self.LEADER_WEIGHT_FRACTION,
        )

    async def _get_reward_recipients(
        self,
        tournament_id: str,
        metagraph: bt.Metagraph,
    ) -> List[RewardRecipient]:
        """Resolve podium places to ``(uid, share)`` pairs.

        Places whose hotkey is missing from the metagraph are skipped; their
        share burns with the unallocated remainder. Returns an empty list only
        when the backend confirms there is no approved winner (or no podium
        hotkey is registered).

        Raises:
            WeightLookupError: If the winner lookup itself failed.
        """
        logger.info("Querying winners from tournament API...")

        try:
            winner_info = await self.api.get_winner_hotkey(tournament_id)
        except Exception as e:
            raise WeightLookupError(f"[{tournament_id}] winner lookup failed: {e}") from e

        if not winner_info.winner_approved:
            logger.info("No winner approved - will burn remainder on UID 0")
            return []

        podium = list(winner_info.winners or [])
        if not podium and winner_info.winner_hotkey:
            podium = [
                PodiumWinnerInfo(
                    place=1,
                    hotkey=winner_info.winner_hotkey,
                    agent_id=winner_info.winner_agent_id,
                    score=winner_info.winner_score,
                    reward_share=1.0,
                )
            ]

        recipients: List[RewardRecipient] = []
        for place in sorted(podium, key=lambda p: p.place):
            if not place.hotkey or place.reward_share <= 0:
                continue
            uid = find_uid_for_hotkey(metagraph, place.hotkey)
            if uid is None:
                logger.warning(
                    f"Place {place.place} hotkey not in metagraph — "
                    f"share {place.reward_share:.1%} burns"
                )
                continue
            logger.info(
                f"Place {place.place} -> UID {uid} "
                f"(share {place.reward_share:.1%})"
            )
            recipients.append((uid, float(place.reward_share)))

        if not recipients:
            logger.info("No podium UIDs resolved — will burn remainder on UID 0")
        return recipients

    async def _set_weight_distribution(
        self,
        weight_map: dict[int, float],
        metagraph: bt.Metagraph,
    ) -> None:
        """
        Set on-chain weights from an arbitrary ``{uid: weight}`` mapping.

        Includes a dedup check via the tournament backend: if the exact same
        weights were already committed on-chain (by this or another validator
        instance sharing the hotkey) within ``DEDUP_WINDOW`` seconds, the
        on-chain call is skipped entirely.

        Args:
            weight_map: Mapping of UID -> weight fraction (should sum to 1.0).
            metagraph: Current metagraph.
        """
        wallet = self._load_wallet()
        netuid = self.config.subnet.subnet_uid
        weight_hash = compute_weight_hash(weight_map)

        # --- dedup check ---
        try:
            latest = await self.api.get_latest_weight_commit(
                validator_hotkey=wallet.hotkey.ss58_address,
                netuid=netuid,
            )
            if latest is not None and latest.weight_hash == weight_hash:
                age = (datetime.now(timezone.utc) - _as_utc(latest.committed_at)).total_seconds()
                if age < self.DEDUP_WINDOW:
                    logger.info(
                        f"Skipping redundant weight set (same hash committed {int(age)}s ago)"
                    )
                    return
        except Exception as e:
            logger.debug(f"Weight commit dedup check failed, proceeding: {e}")

        n = len(metagraph.uids)
        out_of_range = [uid for uid in weight_map if not 0 <= uid < n]
        if out_of_range:
            raise WeightLookupError(f"UIDs {out_of_range} not in metagraph (n={n})")

        uids: List[int] = list(range(n))
        weights: List[float] = [0.0] * n
        for uid, w in weight_map.items():
            weights[uid] = w

        desc = ", ".join(f"UID {u}: {w:.2%}" for u, w in weight_map.items())
        logger.info(f"Setting weights: {desc}")

        max_attempts = self.config.retry.weight_setting_max_attempts
        delay = self.config.retry.weight_setting_initial_delay
        success = False

        for attempt in range(1, max_attempts + 1):
            try:
                subtensor = await self._get_subtensor()
                success, message = await asyncio.wait_for(
                    asyncio.to_thread(
                        subtensor.set_weights,
                        wallet=wallet,
                        netuid=netuid,
                        uids=uids,
                        weights=weights,
                        wait_for_inclusion=True,
                        wait_for_finalization=False,
                    ),
                    timeout=self.SET_WEIGHTS_TIMEOUT,
                )

                if success:
                    logger.info(f"✅ Weights set successfully ({desc})")
                    break
                else:
                    logger.warning(f"Weight setting returned: {message}")

            except Exception as e:
                logger.error(f"Attempt {attempt}/{max_attempts} failed: {e!r}")
                self._reset_subtensor()

            if attempt < max_attempts:
                logger.info(f"Retrying in {delay}s...")
                await asyncio.sleep(delay)
                delay *= 2

        # --- report on success ---
        if success:
            await self.api.report_weight_commit(
                validator_hotkey=wallet.hotkey.ss58_address,
                netuid=netuid,
                weight_hash=weight_hash,
                weight_data={str(uid): w for uid, w in weight_map.items()},
            )
            return

        logger.error(
            f"Failed to set weights after {max_attempts} attempts ({desc}); "
            "leaving existing on-chain weights unchanged"
        )
