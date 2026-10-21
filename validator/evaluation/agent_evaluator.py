"""
Single agent evaluation logic.

Handles the complete evaluation flow for a single agent:
- Clean previous state
- Download and extract agent
- Run evaluation inside a sandboxed Docker container
- Collect and submit results
- Cleanup

"""

import json
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Optional, Any

import yaml

from nepher_core.api import TournamentAPI, Agent, APIError, QuietZoneError
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

    def __init__(self, message: str, recoverable: bool = True, log_output: str = ""):
        self.message = message
        self.recoverable = recoverable
        self.log_output = log_output
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

    MAX_POLICY_SIZE_MB = 2048  # reject files larger than 2 GB
    MAX_AGENT_ARCHIVE_BYTES = 4 * 1024**3  # download cap (compressed)
    MAX_AGENT_UNZIPPED_BYTES = 8 * 1024**3  # extraction cap (uncompressed)
    MAX_LOG_ARCHIVE_BYTES = 200 * 1024**2  # eval-log upload cap (uncompressed)

    def __init__(
        self,
        config: ValidatorConfig,
        api: TournamentAPI,
        validator_hotkey: str,
    ):
        self.config = config
        self.api = api
        self.validator_hotkey = validator_hotkey

        # Base workspace (shared). Per-tournament artifacts live under
        # workspace/tournaments/<id>/ but remain *under* the base workspace so
        # the sandbox HOST_WORKSPACE path mapping keeps working.
        self.workspace = config.paths.workspace

        # Per-evaluation context (set by _set_tournament_context). Evaluation is
        # sequential (one agent at a time), so storing the current tournament's
        # paths on the instance is safe.
        self.tn_workspace: Path = self.workspace
        self.registry_path = self.workspace / "agent_registry"
        self.result_path = self.workspace / "evaluation_result.json"

        # Sandbox runner for isolated evaluation. Workspace is the *base* dir so
        # that Docker volume host-path translation continues to resolve.
        self.sandbox = SandboxRunner(
            workspace=self.workspace,
            env_cache_path=config.paths.env_cache,
        )

    def _set_tournament_context(self, tournament_id: str) -> None:
        """Point per-evaluation paths at the given tournament's workspace."""
        self.tn_workspace = self.config.paths.tournament_workspace(tournament_id)
        self.tn_workspace.mkdir(parents=True, exist_ok=True)
        self.registry_path = self.tn_workspace / "agent_registry"
        self.result_path = self.tn_workspace / "evaluation_result.json"

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
        self._set_tournament_context(tournament_id)
        task_module = self._get_task_module()

        try:
            logger.info(f"Evaluating agent: {agent.id} (tournament {tournament_id})")
            await self._clean_previous_state()
            await self._prepare_agent(agent)

            await self.api.set_evaluation_in_progress(
                tournament_id=tournament_id,
                agent_id=agent.id,
                validator_hotkey=self.validator_hotkey,
            )

            result = await self._run_sandboxed_evaluation(task_module)

        except (EvaluationError, QuietZoneError):
            raise
        except SandboxError as e:
            logger.error(f"Sandbox evaluation failed: {e}")
            raise EvaluationError(str(e), recoverable=e.recoverable, log_output=e.log_output)
        except Exception as e:
            # Unknown errors (API, Docker CLI, filesystem) are validator-side:
            # never record them as the agent's score.
            logger.error(f"Evaluation failed: {e}")
            raise EvaluationError(str(e), recoverable=True)
        finally:
            await self._cleanup(tournament_id)

        # Submit outside the EvaluationError try-block so that API
        # submission errors (e.g. "already submitted") are never wrapped
        # as EvaluationError and never trigger submit_failed_evaluation
        # in the orchestrator.
        try:
            await self._submit_results(tournament_id, agent.id, result)
            logger.info(f"Evaluation complete for agent: {agent.id}")
        except APIError as e:
            logger.warning(f"Result submission failed for agent {agent.id}: {e}")
        except Exception as e:
            logger.error(f"Unexpected submission error for agent {agent.id}: {e}")

    # -- Shared helpers -------------------------------------------------------

    def _get_task_module(self) -> str:
        """Get task module name from the current tournament's task config.

        Reads the per-tournament task_config.yaml (written by setup) so that
        concurrently-active tournaments each use their own task module.
        """
        task_config_path = self.tn_workspace / "task_config.yaml"
        if not task_config_path.exists():
            raise EvaluationError(
                f"Task configuration not found: {task_config_path}",
                recoverable=True,
            )
        with open(task_config_path, "r") as f:
            task_config = yaml.safe_load(f) or {}
        task_module = task_config.get("task_module")
        if not task_module:
            raise EvaluationError(
                "task_module missing from task configuration",
                recoverable=True,
            )
        return task_module

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
            try:
                await self.api.download_agent(
                    agent.id, tmp_path, max_bytes=self.MAX_AGENT_ARCHIVE_BYTES
                )
            except ValueError as e:
                raise EvaluationError(str(e), recoverable=False) from e
            self.registry_path.mkdir(parents=True, exist_ok=True)
            try:
                unzip_file(
                    tmp_path,
                    self.registry_path,
                    max_total_bytes=self.MAX_AGENT_UNZIPPED_BYTES,
                )
            except (ValueError, zipfile.BadZipFile) as e:
                raise EvaluationError(f"Invalid agent archive: {e}", recoverable=False) from e
            logger.info(f"Agent extracted to: {self.registry_path}")
        finally:
            tmp_path.unlink(missing_ok=True)

        self._check_policy_size()

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

    def _check_policy_size(self) -> None:
        """Reject oversized policy checkpoints before spawning a sandbox."""
        policy_file = self.registry_path / "best_policy" / "best_policy.pt"
        if not policy_file.is_file():
            return

        size_mb = policy_file.stat().st_size / (1024 * 1024)
        if size_mb > self.MAX_POLICY_SIZE_MB:
            raise EvaluationError(
                f"Policy file too large ({size_mb:.0f} MB, "
                f"limit {self.MAX_POLICY_SIZE_MB} MB)",
                recoverable=False,
            )

    def _build_eval_config(self) -> Path:
        """
        Build an evaluation config YAML with the agent's policy_path injected.

        The policy_path uses the sandbox-internal mount path, not the host path.
        """
        task_config_path = self.tn_workspace / "task_config.yaml"
        if not task_config_path.exists():
            raise EvaluationError(
                f"Task config not found: {task_config_path}",
                recoverable=True,
            )

        with open(task_config_path, "r") as f:
            config_data = yaml.safe_load(f)

        # Guard: task_type must be explicit in the stored config. If it is
        # absent, EvalConfig will silently fall back to its dataclass default
        # ("navigation.leatherback"), producing a confusing scorer mismatch
        # when scoring_version is set for a different task family.
        if not config_data.get("task_type"):
            raise EvaluationError(
                f"task_config.yaml for tournament {self.tn_workspace.name} is missing "
                f"'task_type'. Add the correct task_type (e.g. 'navigation.spot') to "
                f"the tournament's eval config in the database.",
                recoverable=True,
            )

        policy_path = self._resolve_policy_path()
        config_data["policy_path"] = policy_path
        logger.info(f"Resolved policy_path (sandbox): {policy_path}")

        eval_config_path = self.tn_workspace / "eval_config.yaml"
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

        # Run in sandbox (pass the per-tournament task config explicitly)
        result = await self.sandbox.run_evaluation(
            agent_registry=self.registry_path,
            eval_config_path=eval_config_path,
            task_module=task_module,
            timeout=timeout,
            whitelist_domains=whitelist_domains,
            task_config_path=self.tn_workspace / "task_config.yaml",
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
        if log_path.is_symlink() or not log_path.is_dir():
            return None

        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            archive_path = Path(tmp.name)

        try:
            zip_directory(log_path, archive_path, max_total_bytes=self.MAX_LOG_ARCHIVE_BYTES)
        except ValueError as e:
            logger.warning(f"Skipping eval-log upload: {e}")
            archive_path.unlink(missing_ok=True)
            return None
        return archive_path

    async def _cleanup(self, tournament_id: str) -> None:
        """Remove all evaluation artifacts and clear in-progress status."""
        logger.debug("Cleaning up after evaluation")

        self.result_path.unlink(missing_ok=True)
        eval_config = self.tn_workspace / "eval_config.yaml"
        eval_config.unlink(missing_ok=True)

        clean_directory(self.registry_path)

        try:
            await self.api.clear_evaluation_in_progress(
                tournament_id=tournament_id,
                validator_hotkey=self.validator_hotkey,
            )
        except Exception as e:
            logger.warning(f"Failed to clear in-progress status: {e}")
