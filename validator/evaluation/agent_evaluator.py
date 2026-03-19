"""
Single agent evaluation logic.

Handles the complete evaluation flow for a single agent:
- Clean previous state
- Download and extract agent
- Run evaluation inside a sandboxed Docker container
- Collect and submit results
- Cleanup

"""

import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional, Any

import yaml

from nepher_core.api import TournamentAPI, Agent
from nepher_core.config import ValidatorConfig
from nepher_core.utils.helpers import (
    unzip_file,
    clean_directory,
    zip_directory,
)
from nepher_core.utils.logging import get_logger
from validator.evaluation.sandbox import SandboxRunner, SandboxError

logger = get_logger(__name__)


class EvaluationError(Exception):
    """Raised when evaluation fails."""

    def __init__(self, message: str, recoverable: bool = True):
        self.message = message
        self.recoverable = recoverable
        super().__init__(message)


class AgentEvaluator:
    """
    Handles evaluation of a single agent via sandboxed Docker containers.

    Follows the evaluation flow:
    1. Clean previous state
    2. Download and prepare agent
    3. Mark evaluation in-progress
    4. Run evaluation in sandbox container
    5. Submit results
    6. Cleanup
    """

    MODEL_VALIDATION_TIMEOUT = 120  # seconds — fast-fail on corrupt/unloadable checkpoints
    MAX_POLICY_SIZE_MB = 2048  # reject files larger than 2 GB

    def __init__(
        self,
        config: ValidatorConfig,
        api: TournamentAPI,
        validator_hotkey: str,
    ):
        self.config = config
        self.api = api
        self.validator_hotkey = validator_hotkey

        # Paths
        self.workspace = config.paths.workspace
        self.registry_path = self.workspace / "agent_registry"
        self.result_path = self.workspace / "evaluation_result.json"

        # Sandbox runner for isolated evaluation
        self.sandbox = SandboxRunner(
            workspace=self.workspace,
            env_cache_path=config.paths.env_cache,
        )

    # -- Public API -----------------------------------------------------------

    async def evaluate(
        self,
        tournament_id: str,
        agent: Agent,
    ) -> None:
        """
        Run the complete evaluation pipeline for an agent.

        Raises:
            EvaluationError: If any step fails.
        """
        task_module = self._get_task_module()

        try:
            logger.info(f"Evaluating agent: {agent.id}")
            await self._clean_previous_state()
            await self._prepare_agent(agent)

            await self.api.set_evaluation_in_progress(
                tournament_id=tournament_id,
                agent_id=agent.id,
                validator_hotkey=self.validator_hotkey,
            )

            result = await self._run_sandboxed_evaluation(task_module)
            await self._submit_results(tournament_id, agent.id, result)

            logger.info(f"Evaluation complete for agent: {agent.id}")

        except EvaluationError:
            raise
        except SandboxError as e:
            logger.error(f"Sandbox evaluation failed: {e}")
            raise EvaluationError(str(e), recoverable=e.recoverable)
        except Exception as e:
            logger.error(f"Evaluation failed: {e}")
            raise EvaluationError(str(e), recoverable=True)
        finally:
            await self._cleanup(tournament_id)

    # -- Shared helpers -------------------------------------------------------

    def _get_task_module(self) -> str:
        """Get task module name from config."""
        if self.config.task_config is None:
            raise EvaluationError("Task configuration not loaded", recoverable=False)
        return self.config.task_config.task_module

    # -- Pipeline steps -------------------------------------------------------

    async def _clean_previous_state(self) -> None:
        """Remove artifacts from any previous evaluation."""
        logger.debug("Cleaning previous evaluation state")
        clean_directory(self.registry_path)
        self.result_path.unlink(missing_ok=True)
        # Clean leftover sandbox dirs from crashed runs
        sandbox_base = self.workspace / "sandbox"
        if sandbox_base.exists():
            shutil.rmtree(sandbox_base, ignore_errors=True)

    async def _prepare_agent(self, agent: Agent) -> None:
        """Download and extract agent archive."""
        logger.info(f"Downloading agent: {agent.id}")

        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp_path = Path(tmp.name)

        try:
            await self.api.download_agent(agent.id, tmp_path)
            self.registry_path.mkdir(parents=True, exist_ok=True)
            unzip_file(tmp_path, self.registry_path)
            logger.info(f"Agent extracted to: {self.registry_path}")
        finally:
            tmp_path.unlink(missing_ok=True)

    def _resolve_policy_path(self) -> Optional[str]:
        """
        Resolve the path to the agent's best policy checkpoint.

        Returns the path as it will appear INSIDE the sandbox container,
        since agent files are mounted at /sandbox/agent/.
        """
        policy_file = self.registry_path / "best_policy" / "best_policy.pt"
        if policy_file.exists():
            # Return the sandbox-internal path (agent is copied to /app/agent)
            return "/app/agent/best_policy/best_policy.pt"

        logger.warning(f"Policy checkpoint not found: {policy_file}")
        return None

    async def _validate_model_weights(self) -> None:
        """
        Pre-flight check: load the policy checkpoint in an isolated subprocess
        with a tight timeout.  Catches corrupt, incompatible, or adversarial
        .pt files in ~2 minutes instead of burning the full evaluation timeout.
        Skipped when no policy file is present (some tasks don't require one).
        """
        policy_file = self.registry_path / "best_policy" / "best_policy.pt"
        if not policy_file.exists():
            return

        size_mb = policy_file.stat().st_size / (1024 * 1024)
        if size_mb > self.MAX_POLICY_SIZE_MB:
            raise EvaluationError(
                f"Policy file too large ({size_mb:.0f} MB, "
                f"limit {self.MAX_POLICY_SIZE_MB} MB)",
                recoverable=False,
            )

        logger.info(
            f"Validating model weights ({size_mb:.1f} MB, "
            f"timeout {self.MODEL_VALIDATION_TIMEOUT}s)..."
        )

        validation_script = (
            "import sys, torch; "
            "cp = torch.load(sys.argv[1], map_location='cpu', weights_only=True); "
            "t = type(cp).__name__; "
            "n = len(cp) if isinstance(cp, dict) else -1; "
            "print(f'OK type={t} keys={n}')"
        )

        try:
            return_code, stdout, stderr = await run_command_async(
                [sys.executable, "-c", validation_script, str(policy_file)],
                timeout=self.MODEL_VALIDATION_TIMEOUT,
            )
        except asyncio.TimeoutError:
            raise EvaluationError(
                f"Model weight validation timed out after "
                f"{self.MODEL_VALIDATION_TIMEOUT}s — file is likely corrupt "
                f"or not a valid PyTorch checkpoint",
                recoverable=False,
            )

        if return_code != 0:
            detail = (stderr or stdout)[-500:]
            raise EvaluationError(
                f"Model weight validation failed: {detail}",
                recoverable=False,
            )

        logger.info(f"Model weights validated: {stdout.strip()}")

    def _build_eval_config(self) -> Path:
        """
        Build an evaluation config YAML with the agent's policy_path injected.

        The policy_path uses the sandbox-internal mount path, not the host path.
        """
        task_config_path = self.workspace / "task_config.yaml"
        if not task_config_path.exists():
            raise EvaluationError(
                f"Task config not found: {task_config_path}",
                recoverable=False,
            )

        with open(task_config_path, "r") as f:
            config_data = yaml.safe_load(f)

        policy_path = self._resolve_policy_path()
        config_data["policy_path"] = policy_path
        logger.info(f"Resolved policy_path (sandbox): {policy_path}")

        eval_config_path = self.workspace / "eval_config.yaml"
        with open(eval_config_path, "w") as f:
            yaml.dump(config_data, f, default_flow_style=False)

        return eval_config_path

    async def _run_sandboxed_evaluation(self, task_module: str) -> dict[str, Any]:
        """
        Run evaluation inside an isolated sandbox container.

        The sandbox container:
        - Has GPU access for Isaac Sim
        - Has NO wallet access
        - Has NO Docker socket
        - Has NO network access
        - Can only write to the output directory
        """
        logger.info("Running evaluation in sandbox container")

        # Verify Docker/sandbox are available
        await self.sandbox.verify_docker()

        # Build eval config with sandbox-internal paths
        eval_config_path = self._build_eval_config()

        timeout = self.config.retry.evaluation_timeout_seconds

        # Fetch whitelisted domains for sandbox network proxy
        whitelist_domains = await self.api.get_whitelist_domains()

        # Run in sandbox
        result = await self.sandbox.run_evaluation(
            agent_registry=self.registry_path,
            eval_config_path=eval_config_path,
            task_module=task_module,
            timeout=timeout,
            whitelist_domains=whitelist_domains,
        )

        # Save result to canonical location for log archiving
        with open(self.result_path, "w") as f:
            json.dump(result, f)

        logger.info(f"Sandbox evaluation score: {result.get('score', 'N/A')}")
        return result

    async def _submit_results(
        self,
        tournament_id: str,
        agent_id: str,
        result: dict[str, Any],
    ) -> None:
        """Submit evaluation results (and optional logs) to the API."""
        logger.info("Submitting evaluation results")

        log_file = self._create_log_archive(result.get("log_dir"))

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
            if log_file:
                log_file.unlink(missing_ok=True)

    def _create_log_archive(self, log_dir: Optional[str]) -> Optional[Path]:
        """ZIP the log directory if it exists, returning the archive path."""
        if not log_dir:
            return None

        log_path = Path(log_dir)
        if not log_path.exists():
            return None

        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            archive_path = Path(tmp.name)

        zip_directory(log_path, archive_path)
        return archive_path

    async def _cleanup(self, tournament_id: str) -> None:
        """Remove all evaluation artifacts and clear in-progress status."""
        logger.debug("Cleaning up after evaluation")

        self.result_path.unlink(missing_ok=True)
        eval_config = self.workspace / "eval_config.yaml"
        eval_config.unlink(missing_ok=True)

        clean_directory(self.registry_path)

        try:
            await self.api.clear_evaluation_in_progress(
                tournament_id=tournament_id,
                validator_hotkey=self.validator_hotkey,
            )
        except Exception as e:
            logger.warning(f"Failed to clear in-progress status: {e}")
