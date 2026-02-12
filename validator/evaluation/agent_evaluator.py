"""
Single agent evaluation logic.

Handles the complete evaluation flow for a single agent:
- Clean previous state
- Download and extract agent
- Install agent module
- Run evaluation
- Collect and submit results
- Cleanup
"""

import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Optional, Any

from nepher_core.api import TournamentAPI, Agent
from nepher_core.config import ValidatorConfig
from nepher_core.utils.helpers import (
    unzip_file,
    run_command_async,
    clean_directory,
    zip_directory,
    is_module_installed,
)
from nepher_core.utils.logging import get_logger

logger = get_logger(__name__)


class EvaluationError(Exception):
    """Raised when evaluation fails."""
    
    def __init__(self, message: str, recoverable: bool = True):
        self.message = message
        self.recoverable = recoverable
        super().__init__(message)


class AgentEvaluator:
    """
    Handles evaluation of a single agent.
    
    Follows the evaluation flow:
    1. Clean previous state
    2. Download and prepare agent
    3. Mark evaluation in-progress
    4. Install agent module
    5. Run evaluation
    6. Submit results
    7. Cleanup
    """

    def __init__(
        self,
        config: ValidatorConfig,
        api: TournamentAPI,
        validator_hotkey: str,
    ):
        """
        Initialize agent evaluator.
        
        Args:
            config: Validator configuration
            api: Tournament API client
            validator_hotkey: Validator's hotkey for API calls
        """
        self.config = config
        self.api = api
        self.validator_hotkey = validator_hotkey
        
        # Paths
        self.workspace = config.paths.workspace
        self.registry_path = self.workspace / "agent_registry"
        self.result_path = self.workspace / "evaluation_result.json"

    async def evaluate(
        self,
        tournament_id: str,
        agent: Agent,
    ) -> None:
        """
        Run complete evaluation for an agent.
        
        Args:
            tournament_id: Tournament ID
            agent: Agent to evaluate
            
        Raises:
            EvaluationError: If evaluation fails
        """
        task_module = self._get_task_module()
        
        try:
            # Step 1: Clean previous state
            logger.info(f"Evaluating agent: {agent.id}")
            await self._clean_previous_state(task_module)
            
            # Step 2: Download and prepare agent
            await self._prepare_agent(agent)
            
            # Step 3: Mark in-progress
            await self.api.set_evaluation_in_progress(
                tournament_id=tournament_id,
                agent_id=agent.id,
                validator_hotkey=self.validator_hotkey,
            )
            
            # Step 4: Install agent module
            await self._install_agent_module(task_module)
            
            # Step 5: Run evaluation
            result = await self._run_evaluation()
            
            # Step 6: Submit results
            await self._submit_results(tournament_id, agent.id, result)
            
            logger.info(f"✅ Evaluation complete for agent: {agent.id}")
            
        except EvaluationError:
            raise
        except Exception as e:
            logger.error(f"Evaluation failed: {e}")
            raise EvaluationError(str(e), recoverable=True)
        finally:
            # Step 7: Cleanup
            await self._cleanup(tournament_id, task_module)

    def _get_task_module(self) -> str:
        """Get task module name from config."""
        if self.config.task_config is None:
            raise EvaluationError("Task configuration not loaded", recoverable=False)
        return self.config.task_config.task_module

    async def _clean_previous_state(self, task_module: str) -> None:
        """Clean up from any previous evaluation."""
        logger.debug("Cleaning previous evaluation state...")
        
        # Uninstall existing module if installed
        if is_module_installed(task_module):
            logger.debug(f"Uninstalling existing module: {task_module}")
            await run_command_async(
                [sys.executable, "-m", "pip", "uninstall", "-y", task_module],
                timeout=60,
            )
        
        # Clean workspace
        clean_directory(self.registry_path)
        for rp in [
            self.result_path,
            self.config.paths.eval_repo / "evaluation_result.json",
        ]:
            if rp.exists():
                rp.unlink()

    async def _prepare_agent(self, agent: Agent) -> None:
        """Download and extract agent."""
        logger.info(f"Downloading agent: {agent.id}")
        
        # Download agent ZIP
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        
        try:
            await self.api.download_agent(agent.id, tmp_path)
            
            # Extract to registry
            self.registry_path.mkdir(parents=True, exist_ok=True)
            unzip_file(tmp_path, self.registry_path)
            
            logger.info(f"Agent extracted to: {self.registry_path}")
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    async def _install_agent_module(self, task_module: str) -> None:
        """Install the agent's task module."""
        logger.info(f"Installing agent module: {task_module}")
        
        # Find the source directory
        source_path = self.registry_path / "source" / task_module
        if not source_path.exists():
            # Try to find any module in source
            source_dir = self.registry_path / "source"
            if source_dir.exists():
                modules = [d for d in source_dir.iterdir() if d.is_dir()]
                if modules:
                    source_path = modules[0]
                    logger.warning(f"Using module at: {source_path}")
        
        if not source_path.exists():
            raise EvaluationError(
                f"Task module not found: source/{task_module}",
                recoverable=False,
            )
        
        # Install module
        return_code, stdout, stderr = await run_command_async(
            [sys.executable, "-m", "pip", "install", "-e", str(source_path)],
            timeout=300,
        )
        
        if return_code != 0:
            raise EvaluationError(f"Module installation failed: {stderr}")
        
        # Verify installation
        logger.debug("Verifying module installation...")
        list_envs_script = self.registry_path / "scripts" / "list_envs.py"
        if list_envs_script.exists():
            return_code, stdout, stderr = await run_command_async(
                [sys.executable, str(list_envs_script)],
                timeout=60,
            )
            if return_code != 0:
                logger.warning(f"list_envs.py failed: {stderr}")
            else:
                logger.debug(f"Registered environments: {stdout[:200]}...")

    async def _run_evaluation(self) -> dict[str, Any]:
        """Run the actual evaluation."""
        logger.info("Running evaluation...")
        
        # Find evaluate.py script
        eval_script = self.config.paths.eval_repo / "scripts" / "evaluate.py"
        if not eval_script.exists():
            raise EvaluationError(
                f"Evaluation script not found: {eval_script}",
                recoverable=False,
            )
        
        # Task config path
        task_config_path = self.workspace / "task_config.yaml"
        
        # Run evaluation (cwd = eval_repo)
        eval_cwd = self.config.paths.eval_repo
        timeout = self.config.retry.evaluation_timeout_seconds
        return_code, stdout, stderr = await run_command_async(
            [
                sys.executable, str(eval_script),
                "--config", str(task_config_path),
                "--headless",
            ],
            cwd=eval_cwd,
            timeout=timeout,
        )
        
        # Always log output for debugging
        if stdout:
            logger.info(f"Evaluation stdout (last 2000 chars):\n{stdout[-2000:]}")
        if stderr:
            logger.warning(f"Evaluation stderr (last 2000 chars):\n{stderr[-2000:]}")
        
        if return_code != 0:
            raise EvaluationError(
                f"Evaluation script failed (exit={return_code}): {stderr[-500:]}"
            )
        
        # evaluate.py writes evaluation_result.json relative to its cwd (eval_repo)
        cwd_result_path = eval_cwd / "evaluation_result.json"
        
        # Check both possible locations
        result_file = None
        if self.result_path.exists():
            result_file = self.result_path
        elif cwd_result_path.exists():
            result_file = cwd_result_path
            # Move it to the expected workspace location for consistency
            shutil.move(str(cwd_result_path), str(self.result_path))
            result_file = self.result_path
        
        if result_file is None:
            raise EvaluationError("evaluation_result.json not generated")
        
        with open(result_file, "r") as f:
            result = json.load(f)
        
        logger.info(f"Evaluation score: {result.get('score', 'N/A')}")
        return result

    async def _submit_results(
        self,
        tournament_id: str,
        agent_id: str,
        result: dict[str, Any],
    ) -> None:
        """Submit evaluation results to API."""
        logger.info("Submitting evaluation results...")
        
        # Get log directory from result
        log_dir = result.get("log_dir")
        log_file: Optional[Path] = None
        
        if log_dir:
            log_path = Path(log_dir)
            if log_path.exists():
                # Create logs ZIP
                with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                    log_file = Path(tmp.name)
                zip_directory(log_path, log_file)
        
        try:
            await self.api.submit_evaluation(
                tournament_id=tournament_id,
                agent_id=agent_id,
                validator_hotkey=self.validator_hotkey,
                score=result["score"],
                metadata=result.get("metadata", {}),
                summary=result.get("summary", ""),
                log_file=log_file,
            )
        finally:
            if log_file and log_file.exists():
                log_file.unlink()

    async def _cleanup(self, tournament_id: str, task_module: str) -> None:
        """Cleanup after evaluation."""
        logger.debug("Cleaning up after evaluation...")
        
        # Uninstall module
        if is_module_installed(task_module):
            await run_command_async(
                [sys.executable, "-m", "pip", "uninstall", "-y", task_module],
                timeout=60,
            )
        
        # Clean result files (workspace path + eval_repo cwd fallback)
        for rp in [
            self.result_path,
            self.config.paths.eval_repo / "evaluation_result.json",
        ]:
            if rp.exists():
                rp.unlink()
        
        # Clean registry
        clean_directory(self.registry_path)
        
        # Clear in-progress status
        try:
            await self.api.clear_evaluation_in_progress(
                tournament_id=tournament_id,
                validator_hotkey=self.validator_hotkey,
            )
        except Exception as e:
            logger.warning(f"Failed to clear in-progress status: {e}")

