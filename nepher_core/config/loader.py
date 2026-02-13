"""
Configuration loader with environment variable support.

Loads configuration from YAML files and resolves environment variables.
"""

from pathlib import Path
from typing import TypeVar, Type, Optional, Any

import yaml
from pydantic import BaseModel

from nepher_core.config.models import (
    ValidatorConfig,
    MinerConfig,
    TaskConfig,
    resolve_env_vars,
)
from nepher_core.utils.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T", bound=BaseModel)


def _resolve_dict_env_vars(d: dict) -> dict:
    """Recursively resolve environment variables in a dictionary."""
    result = {}
    for key, value in d.items():
        if isinstance(value, dict):
            result[key] = _resolve_dict_env_vars(value)
        elif isinstance(value, list):
            result[key] = [
                _resolve_dict_env_vars(item) if isinstance(item, dict)
                else resolve_env_vars(item) if isinstance(item, str) and "${" in item
                else item
                for item in value
            ]
        elif isinstance(value, str) and "${" in value:
            result[key] = resolve_env_vars(value)
        else:
            result[key] = value
    return result


def load_yaml(path: Path) -> dict[str, Any]:
    """
    Load YAML file and resolve environment variables.
    
    Args:
        path: Path to YAML file
        
    Returns:
        Parsed YAML content with env vars resolved
        
    Raises:
        FileNotFoundError: If file doesn't exist
        yaml.YAMLError: If YAML is invalid
    """
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: {path}")
    
    with open(path, "r", encoding="utf-8") as f:
        content = yaml.safe_load(f)
    
    if content is None:
        return {}
    
    return _resolve_dict_env_vars(content)


def save_yaml(data: dict[str, Any], path: Path) -> None:
    """
    Save dictionary to YAML file.
    
    Args:
        data: Data to save
        path: Path to output file
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
    
    logger.debug(f"Saved configuration to {path}")


COMMON_CONFIG_FILENAME = "common_config.yaml"


def _deep_merge(base: dict, override: dict) -> dict:
    """Deep-merge *override* into *base* (override values win)."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path: Path, config_class: Type[T]) -> T:
    """
    Load and validate configuration from YAML file.

    Automatically loads ``common_config.yaml`` from the same directory
    (if present) and deep-merges the user config on top so that user
    values override the shared defaults.

    Args:
        path: Path to user YAML file (validator_config / miner_config)
        config_class: Pydantic model class to validate against

    Returns:
        Validated configuration instance
    """
    # Load shared common config if it exists alongside the user config
    common_path = path.parent / COMMON_CONFIG_FILENAME
    if common_path.exists():
        base_data = load_yaml(common_path)
        logger.info(f"Loaded common configuration from {common_path}")
    else:
        base_data = {}

    # Load user-specific config
    user_data = load_yaml(path)

    # Deep-merge: user values take precedence over common values
    merged = _deep_merge(base_data, user_data)

    return config_class(**merged)


class ConfigManager:
    """
    Configuration manager for loading and managing configurations.
    
    Handles:
    - Local validator/miner config loading
    - Tournament config downloading and caching
    - Environment variable resolution
    """

    def __init__(self, config_path: Optional[Path] = None):
        """
        Initialize configuration manager.
        
        Args:
            config_path: Path to local config file (optional)
        """
        self._config_path = config_path
        self._config: Optional[ValidatorConfig | MinerConfig] = None
        self._task_config: Optional[TaskConfig] = None

    @property
    def config(self) -> ValidatorConfig | MinerConfig:
        """Get loaded configuration."""
        if self._config is None:
            raise RuntimeError("Configuration not loaded. Call load_validator_config() first.")
        return self._config

    def load_validator_config(self, path: Optional[Path] = None) -> ValidatorConfig:
        """
        Load validator configuration.
        
        Args:
            path: Path to config file (uses constructor path if not provided)
            
        Returns:
            Loaded and validated validator config
        """
        config_path = path or self._config_path
        if config_path is None:
            raise ValueError("No configuration path provided")
        
        logger.info(f"Loading validator configuration from {config_path}")
        self._config = load_config(config_path, ValidatorConfig)
        
        # Ensure workspace exists
        self._config.paths.workspace.mkdir(parents=True, exist_ok=True)
        
        return self._config

    def load_miner_config(self, path: Optional[Path] = None) -> MinerConfig:
        """
        Load miner configuration.
        
        Args:
            path: Path to config file (uses constructor path if not provided)
            
        Returns:
            Loaded and validated miner config
        """
        config_path = path or self._config_path
        if config_path is None:
            raise ValueError("No configuration path provided")
        
        logger.info(f"Loading miner configuration from {config_path}")
        self._config = load_config(config_path, MinerConfig)
        return self._config

    def load_task_config(self, path: Path) -> TaskConfig:
        """
        Load task configuration from file.
        
        Args:
            path: Path to task config file
            
        Returns:
            Loaded and validated task config
        """
        logger.info(f"Loading task configuration from {path}")
        self._task_config = load_config(path, TaskConfig)
        
        # Attach to validator config if loaded
        if isinstance(self._config, ValidatorConfig):
            self._config.task_config = self._task_config
        
        return self._task_config

    def save_task_config(self, config: dict[str, Any], path: Path) -> None:
        """
        Save task configuration to file.
        
        Args:
            config: Configuration dictionary
            path: Output path
        """
        save_yaml(config, path)
        logger.info(f"Saved task configuration to {path}")

    def save_subnet_config(self, config: dict[str, Any], path: Path) -> None:
        """
        Save subnet configuration to file.
        
        Args:
            config: Configuration dictionary
            path: Output path
        """
        save_yaml(config, path)
        logger.info(f"Saved subnet configuration to {path}")

    def get_required_envs(self) -> list[str]:
        """
        Get list of required environment IDs from task config.
        
        Returns:
            List of environment IDs
        """
        if self._task_config is None:
            raise RuntimeError("Task configuration not loaded")
        
        return [scene.env_id for scene in self._task_config.env_scenes]

    @staticmethod
    def from_env() -> "ConfigManager":
        """
        Create ConfigManager from environment variables.
        
        Looks for NEPHER_CONFIG_PATH environment variable.
        
        Returns:
            ConfigManager instance
        """
        import os
        config_path = os.environ.get("NEPHER_CONFIG_PATH")
        if config_path:
            return ConfigManager(Path(config_path))
        return ConfigManager()

