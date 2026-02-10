"""
Weight setting logic for reward phase.

Handles:
- Querying winner from API
- Finding winner UID in metagraph
- Setting weights on chain
- Burning on UID 0 as fallback
"""

import asyncio
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


class WeightSetter:
    """
    Handles weight setting during reward phase.
    
    Responsibilities:
    - Query winner from tournament API
    - Find winner's UID in metagraph
    - Set all weight to winner UID
    - Burn on UID 0 if no winner or winner not found
    """

    BURN_UID = 0  # UID to burn to when no winner

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

    async def run_reward(
        self,
        tournament: Tournament,
        is_reward_period_fn,
    ) -> None:
        """
        Run reward phase - set weights to winner.
        
        Args:
            tournament: Current tournament
            is_reward_period_fn: Function that returns True if in reward
        """
        logger.info("=" * 60)
        logger.info("Starting reward phase")
        logger.info("=" * 60)
        
        # Get metagraph (needed for weight setting)
        metagraph = self._get_metagraph()
        logger.info(f"Loaded metagraph with {len(metagraph.uids)} UIDs")
        
        # Query winner from API
        winner_uid = await self._get_winner_uid(tournament.id, metagraph)
        
        # Set weights
        await self._set_weights(winner_uid, metagraph)
        
        # Wait for reward period to end
        logger.info("Waiting for reward period to end...")
        while is_reward_period_fn():
            await asyncio.sleep(60)
        
        # After reward ends, burn on UID 0
        logger.info("Reward period ended - burning on UID 0")
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
        """
        Set all weight to a single UID.
        
        Args:
            target_uid: UID to give all weight
            metagraph: Current metagraph
        """
        wallet = self._load_wallet()
        subtensor = self._get_subtensor()
        netuid = self.config.subnet.subnet_uid
        
        # Prepare weights - all 0 except target
        uids: List[int] = list(range(len(metagraph.uids)))
        weights: List[float] = [0.0] * len(uids)
        weights[target_uid] = 1.0
        
        logger.info(f"Setting weight to UID {target_uid}...")
        
        # Retry logic for weight setting
        max_attempts = self.config.retry.weight_setting_max_attempts
        delay = self.config.retry.weight_setting_initial_delay
        
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
                    logger.info(f"✅ Weights set successfully to UID {target_uid}")
                    return
                else:
                    logger.warning(f"Weight setting returned: {message}")
                    
            except Exception as e:
                logger.error(f"Attempt {attempt}/{max_attempts} failed: {e}")
            
            if attempt < max_attempts:
                logger.info(f"Retrying in {delay}s...")
                await asyncio.sleep(delay)
                delay *= 2  # Exponential backoff
        
        logger.error(f"Failed to set weights after {max_attempts} attempts")
        
        # If we can't set weights to winner, try to burn
        if target_uid != self.BURN_UID:
            logger.info("Attempting to burn on UID 0 as fallback...")
            weights = [0.0] * len(uids)
            weights[self.BURN_UID] = 1.0
            
            try:
                subtensor.set_weights(
                    wallet=wallet,
                    netuid=netuid,
                    uids=uids,
                    weights=weights,
                    wait_for_inclusion=True,
                )
                logger.info("Fallback: burned on UID 0")
            except Exception as e:
                logger.error(f"Fallback burn also failed: {e}")

