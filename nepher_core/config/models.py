"""Configuration models using Pydantic."""

import os
import re
from pathlib import Path
from typing import Optional, List, Any

from pydantic import BaseModel, Field, field_validator, model_validator


def resolve_env_vars(value: str) -> str:
    """
    Resolve environment variables in a string.
    
    Supports formats:
    - ${VAR} - Required variable
    - ${VAR:-default} - Variable with default value
    """
    pattern = r'\$\{([^}:]+)(?::-([^}]*))?\}'
    
    def replacer(match):
        var_name = match.group(1)
        default = match.group(2)
        
        env_value = os.environ.get(var_name)
        if env_value is not None:
            return env_value
        elif default is not None:
            return default
        else:
            raise ValueError(f"Environment variable '{var_name}' is not set and has no default")
    
    return re.sub(pattern, replacer, value)


class SubnetConfig(BaseModel):
    """Subnet configuration."""
    
    network: str = Field(default="finney", description="Bittensor network")
    subnet_uid: int = Field(default=49, description="Subnet UID")

    @field_validator("network")
    @classmethod
    def validate_network(cls, v: str) -> str:
        valid_networks = ["finney", "test", "local"]
        if v not in valid_networks:
            raise ValueError(f"network must be one of {valid_networks}")
        return v


class TournamentConfig(BaseModel):
    """Tournament API configuration."""
    
    api_url: str = Field(
        default="https://tournament-api.nepher.ai",
        description="Tournament API base URL",
    )
    api_key: str = Field(description="API key for authentication")

    @field_validator("api_key", mode="before")
    @classmethod
    def resolve_api_key(cls, v: str) -> str:
        if v and v.startswith("${"):
            return resolve_env_vars(v)
        return v


class WalletConfig(BaseModel):
    """Bittensor wallet configuration."""
    
    name: str = Field(default="validator", description="Wallet name")
    hotkey: str = Field(default="default", description="Hotkey name")
    path: Optional[str] = Field(default=None, description="Custom wallet path")

    @field_validator("name", "hotkey", mode="before")
    @classmethod
    def resolve_wallet_vars(cls, v: str) -> str:
        if v and v.startswith("${"):
            return resolve_env_vars(v)
        return v


class IsaacConfig(BaseModel):
    """Isaac Lab/Sim version configuration."""
    
    lab_version: str = Field(default="2.3.0", description="Isaac Lab version")
    sim_version: str = Field(default="5.1", description="Isaac Sim version")


class PathsConfig(BaseModel):
    """Paths configuration."""
    
    workspace: Path = Field(default=Path("./workspace"), description="Workspace directory")
    eval_repo: Path = Field(default=Path("./eval-nav"), description="Eval repo path")
    env_cache: Path = Field(default=Path.home() / ".cache" / "nepher", description="Env cache")

    @field_validator("workspace", "eval_repo", "env_cache", mode="before")
    @classmethod
    def resolve_path_vars(cls, v):
        if isinstance(v, str) and v.startswith("${"):
            v = resolve_env_vars(v)
        return Path(v).expanduser().resolve()


class RetryConfig(BaseModel):
    """Retry configuration for various operations."""
    
    # Network retry settings
    network_max_attempts: int = Field(default=3, ge=1)
    network_initial_delay: float = Field(default=1.0, ge=0.1)
    network_max_delay: float = Field(default=30.0, ge=1.0)
    network_backoff_factor: float = Field(default=2.0, ge=1.0)
    
    # Evaluation retry settings
    evaluation_max_attempts: int = Field(default=2, ge=1)
    evaluation_timeout_seconds: int = Field(default=3600, ge=60)  # 1 hour default
    
    # Weight setting retry settings
    weight_setting_max_attempts: int = Field(default=5, ge=1)
    weight_setting_initial_delay: float = Field(default=5.0, ge=1.0)


class EnvScene(BaseModel):
    """Environment scene configuration."""
    
    env_id: str
    scene: str

    @field_validator("scene", mode="before")
    @classmethod
    def coerce_scene_to_str(cls, v: Any) -> str:
        """Accept both int and str scene identifiers from the API."""
        return str(v)


class TaskConfig(BaseModel):
    """Task/evaluation configuration (downloaded from API)."""
    
    model_config = {"extra": "ignore"}
    
    task_name: str
    task_module: str
    env_scenes: List[EnvScene]
    seeds: List[int] = Field(default=[42])
    num_episodes: int = Field(default=10, ge=1)
    scoring_version: str = Field(default="v1")
    
    # Optional fields
    max_steps_per_episode: Optional[int] = None
    max_episode_steps: Optional[int] = None
    num_envs: Optional[int] = None
    log_dir: Optional[str] = None
    enable_logging: bool = Field(default=False)
    render: bool = Field(default=False)


class ValidatorConfig(BaseModel):
    """Complete validator configuration."""
    
    subnet: SubnetConfig = Field(default_factory=SubnetConfig)
    tournament: TournamentConfig
    wallet: WalletConfig = Field(default_factory=WalletConfig)
    isaac: IsaacConfig = Field(default_factory=IsaacConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    
    # Runtime fields (set after loading configs from API)
    task_config: Optional[TaskConfig] = None
    
    @property
    def api_key(self) -> str:
        """Convenience accessor for API key."""
        return self.tournament.api_key
    
    @property
    def api_url(self) -> str:
        """Convenience accessor for API URL."""
        return self.tournament.api_url


class MinerConfig(BaseModel):
    """Miner configuration for submission."""
    
    tournament: TournamentConfig
    wallet: WalletConfig = Field(default_factory=WalletConfig)
    
    @property
    def api_key(self) -> str:
        """Convenience accessor for API key."""
        return self.tournament.api_key
    
    @property
    def api_url(self) -> str:
        """Convenience accessor for API URL."""
        return self.tournament.api_url


# Type alias for generic config
Config = ValidatorConfig | MinerConfig

