"""
Sandbox container runner for isolated agent evaluation.

"""

import asyncio
import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any, Optional

from nepher_core.utils.logging import get_logger

logger = get_logger(__name__)

# Default sandbox image name
DEFAULT_SANDBOX_IMAGE = "nepher-sandbox:latest"

# Container resource limits
DEFAULT_MEMORY_LIMIT = "32g"
DEFAULT_SHM_SIZE = "8g"
DEFAULT_PIDS_LIMIT = 4096


class SandboxError(Exception):
    """Raised when sandbox execution fails."""

    def __init__(self, message: str, recoverable: bool = True):
        self.message = message
        self.recoverable = recoverable
        super().__init__(message)


class SandboxRunner:
    """
    Manages sandboxed Docker containers for agent evaluation.

    Each evaluation spawns an isolated container that:
    1. Receives agent files via volume mount (read-only)
    2. Receives eval config via volume mount (read-only)
    3. Installs agent module, runs evaluation
    4. Writes evaluation_result.json to output volume
    5. Is automatically removed after completion

    IMPORTANT — Docker-in-Docker path mapping:
    The validator runs inside a container but talks to the **host** Docker
    daemon via the mounted socket.  ``docker run -v`` paths must therefore
    be **host** paths, not container-internal paths.  We solve this with
    two env vars:
      HOST_WORKSPACE   – host-side path that maps to /app/workspace
      HOST_ENV_CACHE   – host-side path that maps to the env cache volume
    """

    def __init__(
        self,
        workspace: Path,
        sandbox_image: Optional[str] = None,
        env_cache_path: Optional[Path] = None,
        memory_limit: str = DEFAULT_MEMORY_LIMIT,
        shm_size: str = DEFAULT_SHM_SIZE,
    ):
        self.workspace = workspace
        self.sandbox_image = sandbox_image or os.environ.get(
            "SANDBOX_IMAGE", DEFAULT_SANDBOX_IMAGE
        )
        self.env_cache_path = env_cache_path or Path(
            os.environ.get("NEPHER_CACHE_DIR", str(Path.home() / ".cache" / "nepher"))
        )
        self.memory_limit = memory_limit
        self.shm_size = shm_size

        # Host-side path mapping for Docker-in-Docker volume mounts.
        # Inside the validator container, workspace is at /app/workspace,
        # but docker run -v needs the HOST path since the Docker daemon
        # runs on the host.
        self._host_workspace = os.environ.get("HOST_WORKSPACE")
        self._host_env_cache = os.environ.get("HOST_ENV_CACHE")

        if not self._host_workspace:
            logger.warning(
                "HOST_WORKSPACE not set — assuming validator runs on host "
                "(not inside a container). Volume mounts will use container paths."
            )

        # Sandbox working directories
        self._sandbox_base = workspace / "sandbox"

    async def verify_docker(self) -> None:
        """Verify Docker is accessible and sandbox image exists."""
        returncode, stdout, stderr = await self._run_cmd(["docker", "info"])
        if returncode != 0:
            raise SandboxError(
                "Docker daemon not accessible. Is the socket mounted?",
                recoverable=False,
            )

        returncode, stdout, stderr = await self._run_cmd(
            ["docker", "image", "inspect", self.sandbox_image]
        )
        if returncode != 0:
            raise SandboxError(
                f"Sandbox image '{self.sandbox_image}' not found. "
                f"Build it with: docker build -f docker/Dockerfile.sandbox -t {self.sandbox_image} .",
                recoverable=False,
            )

        logger.info(f"Sandbox image verified: {self.sandbox_image}")

    async def run_evaluation(
        self,
        agent_registry: Path,
        eval_config_path: Path,
        task_module: str,
        timeout: int = 3600,
    ) -> dict[str, Any]:
        """
        Run agent evaluation in an isolated sandbox container.

        Spawns a single container that installs the agent module and
        runs the evaluation. The sandbox has GPU access but no wallet,
        no Docker socket, and dropped Linux capabilities.

        Args:
            agent_registry: Path to extracted agent files
            eval_config_path: Path to eval_config.yaml
            task_module: Name of the task module to install
            timeout: Evaluation timeout in seconds

        Returns:
            Evaluation result dict with score, metadata, summary

        Raises:
            SandboxError: If sandbox execution fails
        """
        sandbox_id = uuid.uuid4().hex[:12]
        container_name = f"nepher-sandbox-{sandbox_id}"

        # Create sandbox directories
        sandbox_dir = self._sandbox_base / sandbox_id
        output_dir = sandbox_dir / "output"
        config_dir = sandbox_dir / "config"

        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            config_dir.mkdir(parents=True, exist_ok=True)

            # Copy eval config to sandbox config dir
            shutil.copy2(eval_config_path, config_dir / "eval_config.yaml")
            task_config = self.workspace / "task_config.yaml"
            if task_config.exists():
                shutil.copy2(task_config, config_dir / "task_config.yaml")

            cmd = self._build_docker_cmd(
                container_name=container_name,
                agent_registry=agent_registry,
                config_dir=config_dir,
                output_dir=output_dir,
                task_module=task_module,
                timeout=timeout,
            )

            logger.info(
                f"Sandbox container: {container_name} "
                f"(image={self.sandbox_image}, task={task_module}, timeout={timeout}s)"
            )

            container_timeout = timeout + 120

            returncode, stdout, stderr = await self._run_cmd(
                cmd, timeout=container_timeout
            )

            if stdout:
                logger.info(f"Sandbox stdout (last 2000 chars):\n{stdout[-2000:]}")
            if stderr:
                logger.warning(f"Sandbox stderr (last 2000 chars):\n{stderr[-2000:]}")

            # Collect result from output directory
            result = self._collect_result(output_dir)

            if returncode != 0:
                logger.warning(f"Sandbox container exited with code {returncode}")
                if result.get("metadata", {}).get("error"):
                    raise SandboxError(
                        f"Sandbox evaluation failed: {result.get('summary', 'unknown error')}"
                    )

            return result

        finally:
            await self._cleanup_container(container_name)

    def _to_host_path(self, container_path: Path) -> str:
        """Translate a container-internal path to the corresponding host path.

        When the validator runs inside a Docker container, paths like
        ``/app/workspace/agent_registry`` exist only inside that container.
        The host Docker daemon needs the *host-side* path for ``-v`` mounts.

        Translation rules:
        - Paths under ``self.workspace`` (e.g. /app/workspace/…) are mapped
          via HOST_WORKSPACE.
        - The env-cache path is mapped via HOST_ENV_CACHE.
        - If HOST_WORKSPACE is not set, we assume the validator runs directly
          on the host and return the path as-is.
        """
        resolved = container_path.resolve()

        if self._host_workspace:
            workspace_resolved = self.workspace.resolve()
            try:
                relative = resolved.relative_to(workspace_resolved)
                return str(Path(self._host_workspace) / relative)
            except ValueError:
                pass  # not under workspace

        if self._host_env_cache:
            cache_resolved = self.env_cache_path.resolve()
            try:
                relative = resolved.relative_to(cache_resolved)
                return str(Path(self._host_env_cache) / relative)
            except ValueError:
                pass

        # Fallback: use the container path as-is (works when not in DinD)
        return str(resolved)

    def _build_docker_cmd(
        self,
        container_name: str,
        agent_registry: Path,
        config_dir: Path,
        output_dir: Path,
        task_module: str,
        timeout: int,
    ) -> list[str]:
        """Build the `docker run` command with security restrictions."""

        # Translate container paths → host paths for volume mounts
        host_agent = self._to_host_path(agent_registry)
        host_config = self._to_host_path(config_dir)
        host_output = self._to_host_path(output_dir)

        logger.info(
            f"Volume mounts: agent={host_agent}, config={host_config}, "
            f"output={host_output}"
        )

        cmd = [
            "docker", "run",
            # Auto-remove on exit
            "--rm",
            # Container name for tracking/cleanup
            "--name", container_name,
            # ── Security restrictions ──
            # Drop all Linux capabilities, then add back only what Isaac Sim
            # needs: DAC_READ_SEARCH is required to follow the _isaac_sim
            # symlink across directories.
            "--cap-drop", "ALL",
            "--cap-add", "DAC_READ_SEARCH",
            # NET_ADMIN is needed by the entrypoint to set up iptables
            # firewall rules. It is dropped via capsh BEFORE miner code runs.
            "--cap-add", "NET_ADMIN",
            # No new privileges (prevent setuid/setgid escalation)
            "--security-opt", "no-new-privileges:true",
            # Resource limits
            "--memory", self.memory_limit,
            "--shm-size", self.shm_size,
            "--pids-limit", str(DEFAULT_PIDS_LIMIT),
            # ── GPU access ──
            "--gpus", "all",
            "--runtime", "nvidia",
            # ── Environment ──
            "-e", f"TASK_MODULE={task_module}",
            "-e", f"EVAL_TIMEOUT={timeout}",
            "-e", "NVIDIA_VISIBLE_DEVICES=all",
            "-e", "NVIDIA_DRIVER_CAPABILITIES=all",
            "-e", "CUDA_MODULE_LOADING=LAZY",
            "-e", "ACCEPT_EULA=Y",
            "-e", "PRIVACY_CONSENT=Y",
            # Tell the nepher library where the environment cache is.
            # The entrypoint symlinks /sandbox/envs → /root/.cache/nepher.
            "-e", "NEPHER_CACHE_DIR=/root/.cache/nepher",
            # ── Volume mounts (using HOST paths for DinD) ──
            # Agent files (READ-ONLY — cannot modify or escape)
            "-v", f"{host_agent}:/sandbox/agent:ro",
            # Eval config (READ-ONLY)
            "-v", f"{host_config}:/sandbox/config:ro",
            # Output directory (WRITE — only place sandbox can write results)
            "-v", f"{host_output}:/sandbox/output",
            # Tmpfs for writable temp directories
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=4g",
            "--tmpfs", "/var/tmp:rw,noexec,nosuid,size=1g",
        ]

        # Mount environment cache if it exists (READ-ONLY)
        if self.env_cache_path.exists():
            host_cache = self._to_host_path(self.env_cache_path)
            cmd.extend([
                "-v", f"{host_cache}:/sandbox/envs:ro",
            ])

        # The sandbox image
        cmd.append(self.sandbox_image)

        return cmd

    def _collect_result(self, output_dir: Path) -> dict[str, Any]:
        """Read evaluation_result.json from sandbox output directory."""
        result_file = output_dir / "evaluation_result.json"

        if not result_file.exists():
            raise SandboxError("Sandbox did not produce evaluation_result.json")

        with open(result_file, "r") as f:
            result = json.load(f)

        # Replace sandbox-internal log_dir with validator-side path.
        # The entrypoint copies eval logs to /sandbox/output/eval_logs/
        # which maps to output_dir/eval_logs on the validator.
        eval_logs = output_dir / "eval_logs"
        if eval_logs.exists():
            result["log_dir"] = str(eval_logs)

        logger.info(f"Sandbox evaluation score: {result.get('score', 'N/A')}")
        return result

    async def _cleanup_container(self, container_name: str) -> None:
        """Force-remove the sandbox container if it still exists."""
        try:
            await self._run_cmd(
                ["docker", "rm", "-f", container_name],
                timeout=30,
            )
        except Exception:
            pass  # Best-effort cleanup

    @staticmethod
    async def _run_cmd(
        cmd: list[str],
        timeout: Optional[int] = None,
    ) -> tuple[int, str, str]:
        """Run a command asynchronously."""
        logger.debug(f"Running: {' '.join(cmd[:10])}...")

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout,
            )
            return (
                process.returncode or 0,
                stdout.decode() if stdout else "",
                stderr.decode() if stderr else "",
            )
        except asyncio.TimeoutError:
            process.kill()
            raise SandboxError(
                f"Sandbox container timed out after {timeout}s",
                recoverable=True,
            )
