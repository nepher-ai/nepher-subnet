"""Generic brain-mode evaluation. Docker is mocked."""

import asyncio
import hashlib
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from miner.brain_layout import validate_brain_agent_structure
from miner.submit import validate_agent_structure
from nepher_core.config.models import TaskConfig
from validator.evaluation.orchestrator import EvaluationOrchestrator
from validator.evaluation.sandbox import SandboxError, SandboxRunner

DIGEST = "a" * 64
IMAGE = f"registry.example/nepher-brain@sha256:{DIGEST}"
PACKAGE = "git+https://example.com/task.git@" + ("b" * 40)


def _task(**overrides):
    payload = {
        "task_name": "tabletop",
        "task_module": "franka_tabletop.tasks",
        "env_scenes": [{"env_id": "env", "scene": "0"}],
    }
    payload.update(overrides)
    return TaskConfig(**payload)


def _runner(tmp_path: Path) -> SandboxRunner:
    return SandboxRunner(
        workspace=tmp_path,
        sandbox_image="nepher-sandbox:isaacsim6.1-lab3.0",
        env_cache_path=tmp_path / "missing-cache",
    )


def _submission(root: Path, digest: str | None = None, extra: bytes = b"weights") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "brain").mkdir()
    (root / "train").mkdir()
    (root / "weights").mkdir()
    payload = extra
    (root / "weights" / "model.bin").write_bytes(payload)
    file_digest = digest if digest is not None else hashlib.sha256(payload).hexdigest()
    (root / "agent.yaml").write_text(
        yaml.safe_dump(
            {
                "entry": "policy:Policy",
                "horizon": 8,
                "action_dim": 8,
                "weights": {"model.bin": file_digest},
            }
        ),
        encoding="utf-8",
    )
    return root


def test_task_config_defaults_and_rejects_unpinned_brain_fields():
    cfg = _task()
    assert cfg.runtime == "in_process"
    assert cfg.brain is None
    brain = {
        "brain_image": IMAGE,
        "task_package": PACKAGE,
        "benchmark_env_id": "franka-tabletop-v1",
    }
    parsed = _task(runtime="brain", brain=brain, env_scenes=[])
    assert parsed.brain.brain_image == IMAGE
    assert parsed.env_scenes == []
    with pytest.raises(ValidationError):
        _task(env_scenes=[])
    with pytest.raises(ValidationError):
        _task(runtime="brain", brain={**brain, "brain_image": "nepher-brain:latest"})
    with pytest.raises(ValidationError):
        _task(runtime="brain", brain={**brain, "task_package": "git+https://example.com/task.git"})
    with pytest.raises(ValidationError):
        _task(runtime="brain")


def test_layout_validation(tmp_path: Path):
    legacy = tmp_path / "legacy"
    (legacy / "best_policy").mkdir(parents=True)
    (legacy / "best_policy" / "best_policy.pt").write_bytes(b"pt")
    (legacy / "source" / "task").mkdir(parents=True)
    assert validate_agent_structure(legacy)[0]

    good = _submission(tmp_path / "good")
    assert validate_brain_agent_structure(good)[0]
    bad = _submission(tmp_path / "bad", digest="0" * 64)
    ok, errors = validate_brain_agent_structure(bad)
    assert not ok
    assert any("sha256 mismatch" in error for error in errors)
    huge = _submission(tmp_path / "huge", extra=b"x" * 2048)
    ok, errors = validate_brain_agent_structure(huge, max_gb=1e-6)
    assert not ok
    assert any("limit" in error for error in errors)


def test_legacy_docker_command_snapshot(tmp_path: Path):
    runner = _runner(tmp_path)
    agent = tmp_path / "agent"
    config = tmp_path / "config"
    output = tmp_path / "output"
    cmd = runner._build_docker_cmd(
        container_name="nepher-sandbox-test",
        agent_registry=agent,
        config_dir=config,
        output_dir=output,
        task_module="spotwaypointnav",
        timeout=3600,
        whitelist_domains=["example.com"],
    )
    assert cmd == [
        "docker", "run",
        "--rm",
        "--name", "nepher-sandbox-test",
        "--cap-drop", "ALL",
        "--cap-add", "DAC_READ_SEARCH",
        "--cap-add", "NET_ADMIN",
        "--cap-add", "SETUID",
        "--cap-add", "SETGID",
        "--cap-add", "SETPCAP",
        "--memory", "32g",
        "--shm-size", "8g",
        "--pids-limit", "4096",
        "--gpus", "all",
        "-e", "TASK_MODULE=spotwaypointnav",
        "-e", "EVAL_TIMEOUT=3600",
        "-e", "NVIDIA_VISIBLE_DEVICES=all",
        "-e", "NVIDIA_DRIVER_CAPABILITIES=all",
        "-e", "CUDA_MODULE_LOADING=LAZY",
        "-e", "ACCEPT_EULA=Y",
        "-e", "PRIVACY_CONSENT=Y",
        "-e", "NEPHER_CACHE_DIR=/root/.cache/nepher",
        "-e", "SANDBOX_WHITELIST=example.com",
        "-v", f"{agent.resolve()}:/sandbox/agent:ro",
        "-v", f"{config.resolve()}:/sandbox/config:ro",
        "-v", f"{output.resolve()}:/sandbox/output",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=4g",
        "--tmpfs", "/var/tmp:rw,noexec,nosuid,size=1g",
        "nepher-sandbox:isaacsim6.1-lab3.0",
    ]


def test_brain_docker_commands(tmp_path: Path):
    runner = _runner(tmp_path)
    submission = tmp_path / "submission"
    sockets = tmp_path / "sockets"
    check = runner._build_brain_check_cmd(submission, IMAGE, 40)
    assert check[:6] == ["docker", "run", "--rm", "--network", "none", "-v"]
    assert check[-3:] == [IMAGE, "nepher-brain-comm", "check"]
    assert f"{submission.resolve()}:/submission:ro" in check
    serve = runner._build_brain_serve_cmd("nepher-brain-1", submission, sockets, IMAGE, 2, 40)
    assert "--network" in serve and "none" in serve
    assert "--read-only" in serve
    assert "--cap-drop" in serve and "ALL" in serve
    assert serve[-4:] == ["nepher-brain-comm", "serve", "--replicas", "2"]
    world = runner._build_docker_cmd(
        container_name="nepher-sandbox-1",
        agent_registry=submission,
        config_dir=tmp_path / "config",
        output_dir=tmp_path / "output",
        task_module="franka_tabletop.tasks",
        timeout=7200,
        mount_agent=False,
        extra_env={"RUNTIME": "brain", "TASK_PACKAGE": PACKAGE},
        extra_mounts=[(str(sockets), "/run/brain")],
    )
    assert "RUNTIME=brain" in world
    assert f"TASK_PACKAGE={PACKAGE}" in world
    assert not any(part.endswith(":/sandbox/agent:ro") for part in world)
    assert f"{sockets}:/run/brain" in world
    assert world[-1] == "nepher-sandbox:isaacsim6.1-lab3.0"


def test_pull_by_digest(tmp_path: Path):
    runner = _runner(tmp_path)

    async def hit(cmd, timeout=None):
        if "--format" in cmd:
            return 0, f"sha256:{DIGEST} [repo@sha256:{DIGEST}]", ""
        return 0, "present", ""

    runner._run_cmd = hit
    asyncio.run(runner.ensure_brain_image(IMAGE))

    async def miss(cmd, timeout=None):
        if cmd[:3] == ["docker", "pull", IMAGE]:
            return 0, "pulled", ""
        if "--format" in cmd:
            return 0, f"sha256:{DIGEST}", ""
        return 1, "", "missing"

    runner._run_cmd = miss
    asyncio.run(runner.ensure_brain_image(IMAGE))

    async def mismatch(cmd, timeout=None):
        if "--format" in cmd:
            return 0, "sha256:" + ("c" * 64), ""
        return 0, "present", ""

    runner._run_cmd = mismatch
    with pytest.raises(SandboxError, match="image_pull_failed"):
        asyncio.run(runner.ensure_brain_image(IMAGE))

    async def pull_fail(cmd, timeout=None):
        if cmd[1] == "pull":
            return 1, "", "denied"
        return 1, "", "missing"

    runner._run_cmd = pull_fail
    with pytest.raises(SandboxError, match="image_pull_failed"):
        asyncio.run(runner.ensure_brain_image(IMAGE))


def test_run_brain_evaluation_command_order(tmp_path: Path):
    runner = _runner(tmp_path)
    submission = _submission(tmp_path / "submission")
    config = tmp_path / "eval.yaml"
    config.write_text("runtime: brain\n", encoding="utf-8")
    calls = []

    async def fake_run(cmd, timeout=None):
        calls.append(cmd)
        for part in cmd:
            if part.endswith(":/sandbox/output"):
                host = part[: -len(":/sandbox/output")]
                Path(host).mkdir(parents=True, exist_ok=True)
                (Path(host) / "evaluation_result.json").write_text(
                    '{"score": 0.5, "summary": "ok", "metadata": {}}',
                    encoding="utf-8",
                )
        return 0, "", ""

    async def fake_wait(socket_dir, replicas, timeout):
        return None

    runner._run_cmd = fake_run
    runner._wait_for_brain_sockets = fake_wait
    result = asyncio.run(
        runner.run_brain_evaluation(
            submission=submission,
            eval_config_path=config,
            brain_image=IMAGE,
            task_package=PACKAGE,
            task_module="franka_tabletop.tasks",
            replicas=1,
            max_submission_gb=40,
        )
    )
    assert result["score"] == 0.5
    kinds = []
    for cmd in calls:
        if "check" in cmd:
            kinds.append("check")
        elif "serve" in cmd:
            kinds.append("serve")
        elif "smoke" in cmd:
            kinds.append("smoke")
        elif cmd[-1] == "nepher-sandbox:isaacsim6.1-lab3.0":
            kinds.append("sandbox")
        elif cmd[:3] == ["docker", "rm", "-f"]:
            kinds.append("rm")
    assert kinds[:4] == ["check", "serve", "smoke", "sandbox"]
    assert kinds[-2:] == ["rm", "rm"]


def test_error_classification():
        classify = EvaluationOrchestrator._classify_error
        assert classify("brain_check_failed: bad hash") == "brain_check_failed"
        assert classify("brain_smoke_failed: mismatch") == "brain_smoke_failed"
        assert classify("brain_timeout: sockets") == "brain_timeout"
        assert classify("image_pull_failed: denied") == "image_pull_failed"
        assert classify("task_package_install_failed: pip") == "task_package_install_failed"
