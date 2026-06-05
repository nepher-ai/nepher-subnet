"""
Weight setting logic for reward phase.

Handles:
- Querying winner from API
- Finding winner UID in metagraph
- Setting weights on chain
- Burning on UID 0 as fallback
- Deduplicating weight sets across CPU/GPU validators sharing a hotkey
"""

import asyncio
import hashlib
from datetime import datetime, timezone
from typing import Optional, List

import bittensor as bt
from bittensor_wallet import Wallet

from nepher_core.api import TournamentAPI, Tournament
from nepher_core.config import ValidatorConfig
from nepher_core.wallet.utils import (
    load_wallet,
    get_subtensor,
    get_metagraph,
    find_uid_for_hotkey,
)
from nepher_core.utils.logging import get_logger

logger = get_logger(__name__)


def compute_weight_hash(weight_map: dict[int, float]) -> str:
    """Deterministic SHA-256 of a weight distribution."""
    canonical = ",".join(f"{uid}:{w}" for uid, w in sorted(weight_map.items()))
    return hashlib.sha256(canonical.encode()).hexdigest()


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

    def _get_subtensor(self) -> bt.Subtensor:
        """Get subtensor connection."""
        if self._subtensor is None:
            self._subtensor = get_subtensor(self.config.subnet.network)
        return self._subtensor

    def _get_metagraph(self) -> bt.Metagraph:
        """Get and cache metagraph."""
        subtensor = self._get_subtensor()
        # Always refresh metagraph for reward
        self._metagraph = get_metagraph(subtensor, self.config.subnet.subnet_uid)
        return self._metagraph

    WEIGHT_SET_INTERVAL = 1800  # Re-set weights every 30 minutes

    async def burn(
        self,
        tournament_id: Optional[str] = None,
        phase: str = "public",
    ) -> None:
        """
        Burn on UID 0, optionally allocating 1% to the preliminary leader.

        When *tournament_id* is provided the method fetches the current
        leaderboard leader from the backend.  If a leader is found and
        present in the metagraph, weights are split as
        ``{leader_uid: 1%, BURN_UID: 99%}``.  Otherwise 100% is burned.

        Args:
            tournament_id: Tournament to look up the leader for.
            phase: Leaderboard phase — ``"public"`` during evaluation,
                   ``"private"`` during review when final scores are available.
        """
        metagraph = self._get_metagraph()

        if tournament_id is not None:
            try:
                leader = await self.api.get_preliminary_leader(tournament_id, phase=phase)
                if leader.leader_hotkey:
                    leader_uid = find_uid_for_hotkey(metagraph, leader.leader_hotkey)
                    if leader_uid is not None:
                        logger.info(
                            f"Preliminary leader UID {leader_uid} "
                            f"(hotkey {leader.leader_hotkey[:16]}…) — "
                            f"allocating {self.LEADER_WEIGHT_FRACTION:.0%} weight"
                        )
                        await self._set_weight_distribution(
                            {
                                leader_uid: self.LEADER_WEIGHT_FRACTION,
                                self.BURN_UID: 1.0 - self.LEADER_WEIGHT_FRACTION,
                            },
                            metagraph,
                        )
                        return
            except Exception as e:
                logger.warning(f"Preliminary leader lookup failed, falling back to full burn: {e}")

        logger.info("Burning on UID 0")
        await self._set_weight_distribution({self.BURN_UID: 1.0}, metagraph)

    async def run_reward(
        self,
        tournament: Tournament,
        is_reward_period_fn,
    ) -> None:
        """
        Run reward phase - set weights to winner every hour.
        
        Args:
            tournament: Current tournament
            is_reward_period_fn: Function that returns True if in reward
        """
        logger.info("=" * 60)
        logger.info("Starting reward phase (weight setting on chain)")
        logger.info("=" * 60)
        
        while await is_reward_period_fn():
            # Refresh metagraph each cycle
            metagraph = self._get_metagraph()
            logger.info(f"Loaded metagraph with {len(metagraph.uids)} UIDs")
            
            # Query winner from API
            winner_uid = await self._get_winner_uid(tournament.id, metagraph)
            
            # Set weights
            await self._set_weights(winner_uid, metagraph)
            
            # Sleep for 1 hour (or until reward period ends)
            logger.info(
                f"Weights set. Next weight-setting in {self.WEIGHT_SET_INTERVAL}s..."
            )
            elapsed = 0
            while elapsed < self.WEIGHT_SET_INTERVAL and await is_reward_period_fn():
                await asyncio.sleep(60)
                elapsed += 60
        
        # After reward ends, burn on UID 0
        logger.info("Reward period ended - burning on UID 0")
        metagraph = self._get_metagraph()
        await self._set_weights(self.BURN_UID, metagraph)
        
        logger.info("=" * 60)
        logger.info("Reward phase complete")
        logger.info("=" * 60)

    async def _get_winner_uid(
        self,
        tournament_id: str,
        metagraph: bt.Metagraph,
    ) -> int:
        """
        Get winner UID from tournament API.
        
        Returns BURN_UID if:
        - No winner approved
        - Winner hotkey not found in metagraph
        
        Args:
            tournament_id: Tournament ID
            metagraph: Current metagraph
            
        Returns:
            UID to set weight to
        """
        logger.info("Querying winner from tournament API...")
        
        try:
            winner_info = await self.api.get_winner_hotkey(tournament_id)
            
            if not winner_info.winner_approved or not winner_info.winner_hotkey:
                logger.info("No winner approved - will burn on UID 0")
                return self.BURN_UID
            
            winner_hotkey = winner_info.winner_hotkey
            logger.info(f"Winner hotkey: {winner_hotkey[:16]}...")
            
            # Find UID for winner hotkey
            winner_uid = find_uid_for_hotkey(metagraph, winner_hotkey)
            
            if winner_uid is None:
                logger.warning(
                    f"Winner hotkey not found in metagraph - will burn on UID 0"
                )
                return self.BURN_UID
            
            logger.info(f"Winner UID: {winner_uid}")
            return winner_uid
            
        except Exception as e:
            logger.error(f"Failed to get winner: {e}")
            logger.info("Falling back to burn on UID 0")
            return self.BURN_UID

    async def _set_weights(
        self,
        target_uid: int,
        metagraph: bt.Metagraph,
    ) -> None:
        """Set all weight to a single UID (convenience wrapper)."""
        await self._set_weight_distribution({target_uid: 1.0}, metagraph)

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
        subtensor = self._get_subtensor()
        netuid = self.config.subnet.subnet_uid
        weight_hash = compute_weight_hash(weight_map)

        # --- dedup check ---
        try:
            latest = await self.api.get_latest_weight_commit(
                validator_hotkey=wallet.hotkey.ss58_address,
                netuid=netuid,
            )
            if (
                latest is not None
                and latest.weight_hash == weight_hash
                and (datetime.now(timezone.utc) - latest.committed_at.replace(tzinfo=timezone.utc)).total_seconds()
                < self.DEDUP_WINDOW
            ):
                age = int(
                    (datetime.now(timezone.utc) - latest.committed_at.replace(tzinfo=timezone.utc)).total_seconds()
                )
                logger.info(
                    f"Skipping redundant weight set (same hash committed {age}s ago)"
                )
                return
        except Exception as e:
            logger.debug(f"Weight commit dedup check failed, proceeding: {e}")

        uids: List[int] = list(range(len(metagraph.uids)))
        weights: List[float] = [0.0] * len(uids)
        for uid, w in weight_map.items():
            weights[uid] = w

        desc = ", ".join(f"UID {u}: {w:.2%}" for u, w in weight_map.items())
        logger.info(f"Setting weights: {desc}")

        max_attempts = self.config.retry.weight_setting_max_attempts
        delay = self.config.retry.weight_setting_initial_delay
        success = False

        for attempt in range(1, max_attempts + 1):
            try:
                success, message = subtensor.set_weights(
                    wallet=wallet,
                    netuid=netuid,
                    uids=uids,
                    weights=weights,
                    wait_for_inclusion=True,
                    wait_for_finalization=False,
                )

                if success:
                    logger.info(f"✅ Weights set successfully ({desc})")
                    break
                else:
                    logger.warning(f"Weight setting returned: {message}")

            except Exception as e:
                logger.error(f"Attempt {attempt}/{max_attempts} failed: {e}")

            if attempt < max_attempts:
                logger.info(f"Retrying in {delay}s...")
                await asyncio.sleep(delay)
                delay *= 2

        # --- report on success ---
        if success:
            try:
                await self.api.report_weight_commit(
                    validator_hotkey=wallet.hotkey.ss58_address,
                    netuid=netuid,
                    weight_hash=weight_hash,
                    weight_data={str(uid): w for uid, w in weight_map.items()},
                )
            except Exception as e:
                logger.debug(f"Failed to report weight commit: {e}")
            return

        logger.error(f"Failed to set weights after {max_attempts} attempts")

        is_pure_burn = list(weight_map.keys()) == [self.BURN_UID]
        if not is_pure_burn:
            logger.info("Attempting to burn on UID 0 as fallback...")
            fallback_weights = [0.0] * len(uids)
            fallback_weights[self.BURN_UID] = 1.0

            try:
                subtensor.set_weights(
                    wallet=wallet,
                    netuid=netuid,
                    uids=uids,
                    weights=fallback_weights,
                    wait_for_inclusion=True,
                )
                logger.info("Fallback: burned on UID 0")
            except Exception as e:
                logger.error(f"Fallback burn also failed: {e}")

