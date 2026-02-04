"""Setup and installation module."""

from validator.setup.installer import (
    SetupManager,
    verify_isaac_installation,
    verify_nepher_installed,
    download_environments,
    setup_eval_repo,
)

__all__ = [
    "SetupManager",
    "verify_isaac_installation",
    "verify_nepher_installed",
    "download_environments",
    "setup_eval_repo",
]

