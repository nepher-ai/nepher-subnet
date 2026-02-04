"""
Nepher Core - Shared library for Nepher Subnet.

This module provides common functionality for both miners and validators:
- API client for tournament backend
- Configuration management
- Wallet utilities
- Common helpers
"""

from nepher_core.api.client import TournamentAPI
from nepher_core.config.loader import ConfigManager
from nepher_core.config.models import (
    SubnetConfig,
    TournamentConfig,
    TaskConfig,
    WalletConfig,
    IsaacConfig,
    PathsConfig,
)
from nepher_core.utils.logging import setup_logging, get_logger

__version__ = "1.0.0"
__all__ = [
    "TournamentAPI",
    "ConfigManager",
    "SubnetConfig",
    "TournamentConfig", 
    "TaskConfig",
    "WalletConfig",
    "IsaacConfig",
    "PathsConfig",
    "setup_logging",
    "get_logger",
]

