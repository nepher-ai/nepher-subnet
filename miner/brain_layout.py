"""Generic brain-submission layout checks. No model-specific rules."""

import os
from pathlib import Path
from typing import List, Optional, Tuple

import yaml

from nepher_core.custody import sha256_file


def validate_brain_agent_structure(
    agent_path: Path,
    max_gb: Optional[float] = None,
    external_weights: bool = False,
) -> Tuple[bool, List[str]]:
    """Check a brain submission: agent.yaml, brain/, weights/ hashes, and train/.

    Model-specific checks stay in the brain image.

    ``external_weights`` is for huggingface tournaments before the weights are downloaded:
    ``weights/`` may be absent or incomplete, while every file that is present must still
    match its declared hash. The size limit is checked by the caller from the sealed manifest.
    """
    errors: List[str] = []
    if not agent_path.is_dir():
        return False, [f"Agent path is not a directory: {agent_path}"]
    config_path = agent_path / "agent.yaml"
    if not config_path.is_file():
        errors.append("missing agent.yaml")
        config = None
    else:
        try:
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            errors.append(f"agent.yaml: {exc}")
            config = None
        if not isinstance(config, dict):
            errors.append("agent.yaml must be a mapping")
            config = None
    if isinstance(config, dict):
        entry = config.get("entry")
        if not isinstance(entry, str) or entry.count(":") != 1 or entry.startswith(":") or entry.endswith(":"):
            errors.append("entry must be 'module:Class'")
        for key in ("horizon", "action_dim"):
            value = config.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                errors.append(f"{key} must be an integer >= 1")
        weights = config.get("weights")
        if not isinstance(weights, dict):
            errors.append("weights must be a mapping of relative path to sha256")
        else:
            _check_weight_files(
                agent_path / "weights", weights, errors, allow_missing=external_weights
            )
    for name in ("brain", "weights", "train"):
        if name == "weights" and external_weights:
            continue
        if not (agent_path / name).is_dir():
            errors.append(f"missing directory: {name}/")
    if max_gb is None and not external_weights:
        raw = os.environ.get("NEPHER_MAX_SUBMISSION_GB", "")
        max_gb = float(raw) if raw else None
    if max_gb is not None and agent_path.is_dir() and not external_weights:
        total = sum(path.stat().st_size for path in agent_path.rglob("*") if path.is_file())
        limit = int(max_gb * (1024 ** 3))
        if total > limit:
            errors.append(f"submission is {total} bytes; limit is {limit} bytes")
    return len(errors) == 0, errors


def _check_weight_files(
    weights_dir: Path,
    declared: dict,
    errors: List[str],
    allow_missing: bool = False,
) -> None:
    for rel, digest in declared.items():
        if not isinstance(rel, str) or not isinstance(digest, str):
            errors.append("weights entries must be strings")
            continue
        if Path(rel).is_absolute() or ".." in Path(rel).parts:
            errors.append(f"weights path must stay inside weights/: {rel}")
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest.lower()):
            errors.append(f"weights digest for {rel} is not 64 hex characters")
    if not weights_dir.is_dir():
        return
    present = {
        path.relative_to(weights_dir).as_posix()
        for path in weights_dir.rglob("*")
        if path.is_file() and not path.name.startswith(".")
    }
    declared_keys = {key for key in declared if isinstance(key, str)}
    for rel in sorted(present - declared_keys):
        errors.append(f"weights file has no hash in agent.yaml: {rel}")
    for rel in sorted(declared_keys - present):
        if not allow_missing:
            errors.append(f"weights file listed in agent.yaml is missing: {rel}")
    for rel in sorted(present & declared_keys):
        digest = sha256_file(weights_dir / rel)
        if digest != str(declared[rel]).lower():
            errors.append(f"sha256 mismatch for weights/{rel}")
