"""Hugging Face weight custody definitions shared by validators and miners.

``nepher_cli/tournament/custody.py`` implements the same fingerprint and seal definitions
and both files are pinned by the same test vectors. See ``docs/huggingface-weight-custody.md``.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

PROOF_FILENAME = "nepher-proof.json"
MODE_INLINE = "inline"
MODE_HUGGINGFACE = "huggingface"
SUBMISSION_MODES = frozenset({MODE_INLINE, MODE_HUGGINGFACE})

# ``custody_status`` values that an agent can carry in a huggingface tournament.
STATUS_SEALED = "sealed"
STATUS_ARCHIVED = "archived"

_FINGERPRINT_PREFIX = "nepher-weights-v1\n"
_SEAL_PREFIX = "nepher-seal-v1"
_LEASE_PREFIX = "weight-lease"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class CustodyError(ValueError):
    """The submission or its weights violate the custody rules."""


def is_sha256(value: object) -> bool:
    """True for a 64-character lowercase hex digest."""
    return isinstance(value, str) and bool(_SHA256_RE.fullmatch(value))


def sha256_file(path: Path, chunk_size: int = 1 << 20) -> str:
    """Return the SHA-256 hex digest of *path*, reading in chunks."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def weights_fingerprint(weights: Mapping[str, str], base_digests: Iterable[str] = ()) -> str:
    """Return the fingerprint used to detect a duplicate submission.

    Digests on the public base-checkpoint allowlist are left out when the submission also
    contains other files, so miners can share that checkpoint. A submission that is only
    the public checkpoint is fingerprinted in full: the first one is accepted and a later
    copy of the same files collides.

    Raises:
        CustodyError: If there are no weight files.
    """
    if not weights:
        raise CustodyError("weights contain no files")
    base = {digest.lower() for digest in base_digests}
    all_digests = sorted({digest.lower() for digest in weights.values()})
    novel = [digest for digest in all_digests if digest not in base]
    claimed = novel or all_digests
    return hashlib.sha256((_FINGERPRINT_PREFIX + "\n".join(claimed)).encode()).hexdigest()


def seal_message(
    miner_hotkey: str,
    tournament_id: str,
    fingerprint: str,
    hf_repo: str,
    hf_revision: str,
    timestamp: int,
) -> str:
    """Return the exact string the miner hotkey signs to seal a submission."""
    return (
        f"{_SEAL_PREFIX}:{miner_hotkey}:{tournament_id}:{fingerprint}"
        f":{hf_repo}:{hf_revision}:{timestamp}"
    )


def lease_message(validator_hotkey: str, agent_id: str, timestamp: int | None = None) -> str:
    """Return the string a validator hotkey signs to request a weight lease."""
    import time

    stamp = int(time.time()) if timestamp is None else timestamp
    return f"{_LEASE_PREFIX}:{validator_hotkey}:{agent_id}:{stamp}"


def proof_document(tournament_id: str, miner_hotkey: str) -> dict[str, Any]:
    """Return the proof document a miner commits to the Hugging Face repo."""
    return {"version": 1, "tournament_id": tournament_id, "miner_hotkey": miner_hotkey}


def safe_relative_path(rel: str) -> PurePosixPath:
    """Return *rel* as a relative POSIX path that stays inside its parent directory.

    Raises:
        CustodyError: For empty, absolute, drive-qualified, or ``..`` paths.
    """
    if not isinstance(rel, str) or not rel or "\\" in rel or "\x00" in rel:
        raise CustodyError(f"unsafe weights path: {rel!r}")
    path = PurePosixPath(rel)
    if path.is_absolute() or ".." in path.parts or ":" in path.parts[0]:
        raise CustodyError(f"unsafe weights path: {rel!r}")
    return path


def declared_matches_manifest(
    declared: Mapping[str, Any],
    manifest: Mapping[str, str],
) -> list[str]:
    """Compare ``agent.yaml`` weights with the sealed manifest. Returns the differences."""
    declared_norm = {key: str(value).lower() for key, value in declared.items()}
    manifest_norm = {key: value.lower() for key, value in manifest.items()}
    declared_keys, sealed_keys = set(declared_norm), set(manifest_norm)
    errors = [
        f"agent.yaml lists a file that was not sealed: {rel}"
        for rel in sorted(declared_keys - sealed_keys)
    ]
    errors += [
        f"sealed file is missing from agent.yaml: {rel}"
        for rel in sorted(sealed_keys - declared_keys)
    ]
    errors += [
        f"agent.yaml hash differs from the sealed hash for {rel}"
        for rel in sorted(declared_keys & sealed_keys)
        if declared_norm[rel] != manifest_norm[rel]
    ]
    return errors
