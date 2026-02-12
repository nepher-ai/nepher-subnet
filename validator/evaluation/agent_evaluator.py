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

import yaml

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
        self.config = config
        self.api = api
        self.validator_hotkey = validator_hotkey

        # Paths
        self.workspace = config.paths.workspace
        self.registry_path = self.workspace / "agent_registry"
        self.result_path = self.workspace / "evaluation_result.json"

    # ── Public API ───────────────────────────────────────────────────────

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
            await self._clean_previous_state(task_module)
            await self._prepare_agent(agent)

            await self.api.set_evaluation_in_progress(
                tournament_id=tournament_id,
                agent_id=agent.id,
                validator_hotkey=self.validator_hotkey,
            )

            await self._install_agent_module(task_module)
            result = await self._run_evaluation()
            await self._submit_results(tournament_id, agent.id, result)

            logger.info(f"Evaluation complete for agent: {agent.id}")

        except EvaluationError:
            raise
        except Exception as e:
            logger.error(f"Evaluation failed: {e}")
            raise EvaluationError(str(e), recoverable=True)
        finally:
            await self._cleanup(tournament_id, task_module)

    # ── Shared helpers ───────────────────────────────────────────────────

    def _get_task_module(self) -> str:
        """Get task module name from config."""
        if self.config.task_config is None:
            raise EvaluationError("Task configuration not loaded", recoverable=False)
        return self.config.task_config.task_module

    @property
    def _result_file_locations(self) -> list[Path]:
        """All possible locations where evaluation_result.json may appear."""
        return [
            self.result_path,
            self.config.paths.eval_repo / "evaluation_result.json",
        ]

    async def _uninstall_module(self, task_module: str) -> None:
        """Uninstall the task module if currently installed."""
        if is_module_installed(task_module):
            logger.debug(f"Uninstalling module: {task_module}")
            await run_command_async(
                [sys.executable, "-m", "pip", "uninstall", "-y", task_module],
                timeout=60,
            )

    def _remove_files(self, *paths: Path) -> None:
        """Remove each file that exists."""
        for path in paths:
            path.unlink(missing_ok=True)

    # ── Pipeline steps ───────────────────────────────────────────────────

    async def _clean_previous_state(self, task_module: str) -> None:
        """Remove artifacts from any previous evaluation."""
        logger.debug("Cleaning previous evaluation state")
        await self._uninstall_module(task_module)
        clean_directory(self.registry_path)
        self._remove_files(*self._result_file_locations)

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

    async def _install_agent_module(self, task_module: str) -> None:
        """Install the agent's task module via pip."""
        logger.info(f"Installing agent module: {task_module}")

        source_path = self._find_module_source(task_module)

        return_code, _, stderr = await run_command_async(
            [sys.executable, "-m", "pip", "install", "-e", str(source_path)],
            timeout=300,
        )
        if return_code != 0:
            raise EvaluationError(f"Module installation failed: {stderr}")

        await self._verify_installation()

    def _find_module_source(self, task_module: str) -> Path:
        """Locate the module source directory inside the registry."""
        source_path = self.registry_path / "source" / task_module

        if not source_path.exists():
            # Fall back to the first directory under source/
            source_dir = self.registry_path / "source"
            if source_dir.exists():
                candidates = [d for d in source_dir.iterdir() if d.is_dir()]
                if candidates:
                    source_path = candidates[0]
                    logger.warning(f"Expected module not found; using: {source_path}")

        if not source_path.exists():
            raise EvaluationError(
                f"Task module not found: source/{task_module}",
                recoverable=False,
            )
        return source_path

    async def _verify_installation(self) -> None:
        """Run list_envs.py if present to verify the module installed correctly."""
        list_envs_script = self.registry_path / "scripts" / "list_envs.py"
        if not list_envs_script.exists():
            return

        return_code, stdout, stderr = await run_command_async(
            [sys.executable, str(list_envs_script)],
            timeout=60,
        )
        if return_code != 0:
            logger.warning(f"list_envs.py failed: {stderr}")
        else:
            logger.debug(f"Registered environments: {stdout[:200]}")

    def _resolve_policy_path(self) -> Optional[str]:
        """
        Resolve the path to the agent's best policy checkpoint.

        Expected location: agent_registry/best_policy/best_policy.pt
        """
        policy_file = self.registry_path / "best_policy" / "best_policy.pt"
        if policy_file.exists():
            return str(policy_file.resolve())

        logger.warning(f"Policy checkpoint not found: {policy_file}")
        return None

    def _build_eval_config(self) -> Path:
        """
        Build an evaluation config YAML with the agent's policy_path injected.

        Reads the base task_config.yaml and adds the resolved policy_path so
        evaluate.py loads the agent's trained policy.
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
        logger.info(f"Resolved policy_path: {policy_path}")

        eval_config_path = self.workspace / "eval_config.yaml"
        with open(eval_config_path, "w") as f:
            yaml.dump(config_data, f, default_flow_style=False)

        return eval_config_path

    async def _run_evaluation(self) -> dict[str, Any]:
        """Execute the evaluation script and return the result dict."""
        logger.info("Running evaluation")

        eval_script = self.config.paths.eval_repo / "scripts" / "evaluate.py"
        if not eval_script.exists():
            raise EvaluationError(
                f"Evaluation script not found: {eval_script}",
                recoverable=False,
            )

        eval_config_path = self._build_eval_config()
        eval_cwd = self.config.paths.eval_repo
        timeout = self.config.retry.evaluation_timeout_seconds

        # Bootstrap forces spawn multiprocessing and forwards argv to evaluate.py
        bootstrap = (
            "import multiprocessing, sys;"
            " multiprocessing.set_start_method('spawn', force=True);"
            " sys.argv = sys.argv[1:];"
            " import runpy; runpy.run_path(sys.argv[0], run_name='__main__')"
        )

        return_code, stdout, stderr = await run_command_async(
            [
                sys.executable, "-c", bootstrap,
                str(eval_script),
                "--config", str(eval_config_path),
                "--headless",
            ],
            cwd=eval_cwd,
            timeout=timeout,
        )

        if stdout:
            logger.info(f"Evaluation stdout (last 2000 chars):\n{stdout[-2000:]}")
        if stderr:
            logger.warning(f"Evaluation stderr (last 2000 chars):\n{stderr[-2000:]}")

        if return_code != 0:
            raise EvaluationError(
                f"Evaluation script failed (exit={return_code}): {stderr[-500:]}"
            )

        return self._collect_result(eval_cwd)

    def _collect_result(self, eval_cwd: Path) -> dict[str, Any]:
        """
        Locate and read evaluation_result.json.

        The file may appear in the workspace or eval_repo cwd.  If found in the
        cwd fallback location it is moved to the canonical workspace path.
        """
        cwd_result = eval_cwd / "evaluation_result.json"

        if not self.result_path.exists() and cwd_result.exists():
            shutil.move(str(cwd_result), str(self.result_path))

        if not self.result_path.exists():
            raise EvaluationError("evaluation_result.json not generated")

        with open(self.result_path, "r") as f:
            result = json.load(f)

        logger.info(f"Evaluation score: {result.get('score', 'N/A')}")
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

    async def _cleanup(self, tournament_id: str, task_module: str) -> None:
        """Remove all evaluation artifacts and clear in-progress status."""
        logger.debug("Cleaning up after evaluation")

        await self._uninstall_module(task_module)

        self._remove_files(
            *self._result_file_locations,
            self.workspace / "eval_config.yaml",
        )

        clean_directory(self.registry_path)

        try:
            await self.api.clear_evaluation_in_progress(
                tournament_id=tournament_id,
                validator_hotkey=self.validator_hotkey,
            )
        except Exception as e:
            logger.warning(f"Failed to clear in-progress status: {e}")
