"""
Evaluation orchestrator.

Manages the evaluation loop that processes all pending agents
during the evaluation period.
"""

import asyncio
from typing import Optional

from nepher_core.api import TournamentAPI, Tournament, QuietZoneError
from nepher_core.config import ValidatorConfig
from nepher_core.utils.logging import get_logger
from validator.evaluation.agent_evaluator import AgentEvaluator, EvaluationError

logger = get_logger(__name__)


class EvaluationOrchestrator:
    """
    Orchestrates evaluation of all pending agents.
    
    Manages:
    - Fetching pending agents
    - Evaluating each agent
    - Handling failures gracefully
    - Respecting evaluation period
    """

    POLL_INTERVAL = 60  # seconds between checks for new agents
    AGENT_DELAY = 5  # seconds between agent evaluations

    def __init__(
        self,
        config: ValidatorConfig,
        api: TournamentAPI,
        validator_hotkey: str,
    ):
        """
        Initialize orchestrator.
        
        Args:
            config: Validator configuration
            api: Tournament API client
            validator_hotkey: Validator's hotkey
        """
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

    async def run_evaluation_loop(
        self,
        tournament: Tournament,
        is_evaluation_period_fn,
        phase: str = "private",
    ) -> None:
        """
        Run the main evaluation loop.
        
        Continues until evaluation period ends.
        
        Args:
            tournament: Current tournament
            is_evaluation_period_fn: Function that returns True if in evaluation period
            phase: Evaluation phase ('public' or 'private')
        """
        logger.info("=" * 60)
        logger.info(f"Starting {phase} evaluation loop")
        logger.info("=" * 60)
        
        while await is_evaluation_period_fn():
            try:
                await self._process_pending_agents(tournament, phase=phase)
            except QuietZoneError:
                logger.info("Quiet zone reached — stopping evaluation loop")
                break
            except Exception as e:
                logger.error(f"Error in evaluation loop: {e}")
                await asyncio.sleep(self.POLL_INTERVAL)
        
        logger.info("=" * 60)
        logger.info(f"Evaluation loop ({phase}) complete. Stats: {self.stats}")
        logger.info("=" * 60)

    async def _process_pending_agents(self, tournament: Tournament, phase: str = "private") -> None:
        """Process all pending agents."""
        logger.info(f"Checking for pending agents (phase={phase})...")
        response = await self.api.get_pending_agents(
            tournament_id=tournament.id,
            validator_hotkey=self.validator_hotkey,
            phase=phase,
        )
        
        agents = response.agents
        
        if not agents:
            logger.info(f"No pending agents. Waiting {self.POLL_INTERVAL}s...")
            await asyncio.sleep(self.POLL_INTERVAL)
            return
        
        logger.info(f"Found {len(agents)} pending agents")
        
        # Process each agent
        for agent in agents:
            try:
                await self.evaluator.evaluate(
                    tournament_id=tournament.id,
                    agent=agent,
                )
                self._evaluated_count += 1
                
            except QuietZoneError:
                raise
                
            except EvaluationError as e:
                logger.error(f"Evaluation failed for agent {agent.id}: {e.message}")
                self._failed_count += 1
                
                try:
                    await self.api.submit_failed_evaluation(
                        tournament_id=tournament.id,
                        agent_id=agent.id,
                        validator_hotkey=self.validator_hotkey,
                        error_reason=e.message,
                    )
                except QuietZoneError:
                    raise
                except Exception as submit_error:
                    logger.error(f"Failed to submit failure: {submit_error}")
                    
            except Exception as e:
                logger.error(f"Unexpected error evaluating agent {agent.id}: {e}")
                self._failed_count += 1
            
            await asyncio.sleep(self.AGENT_DELAY)

    def reset_stats(self) -> None:
        """Reset evaluation statistics."""
        self._evaluated_count = 0
        self._failed_count = 0

