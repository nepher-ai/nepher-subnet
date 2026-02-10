"""API response models."""

from datetime import datetime
from typing import Optional, List, Any
from pydantic import AliasChoices, BaseModel, Field


class Tournament(BaseModel):
    """Tournament model."""

    id: str
    status: str  # pending, active, review, reward, done, cancelled
    
    # Optional fields with defaults
    name: Optional[str] = None
    network: str = "finney"
    subnet_uid: int = 49

    # Timestamps (Unix timestamps) - optional to handle varying API responses
    contest_start_time: Optional[int] = None
    grace_window_start_time: Optional[int] = None
    contest_end_time: Optional[int] = None
    evaluation_end_time: Optional[int] = None
    reward_end_time: Optional[int] = None

    # Optional fields
    description: Optional[str] = None
    task_name: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    
    # Allow extra fields from API
    class Config:
        extra = "ignore"


class Agent(BaseModel):
    """Agent submission model."""

    # API may return either 'id' or 'agent_id'
    id: str = Field(..., validation_alias=AliasChoices("id", "agent_id"))
    tournament_id: Optional[str] = None
    miner_hotkey: Optional[str] = None
    status: Optional[str] = None  # pending, evaluating, evaluated, failed
    
    # Optional fields
    name: Optional[str] = None
    file_path: Optional[str] = None
    file_size: Optional[int] = None
    checksum: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    message: Optional[str] = None  # Upload response may include a message
    
    class Config:
        extra = "ignore"


class AgentListResponse(BaseModel):
    """Response for agent list endpoints."""

    agents: List[Agent]
    total: int = 0
    limit: int = 0
    offset: int = 0
    
    class Config:
        extra = "ignore"


class Evaluation(BaseModel):
    """Evaluation result model."""

    id: str
    agent_id: Optional[str] = None
    tournament_id: Optional[str] = None
    validator_hotkey: Optional[str] = None
    status: Optional[str] = None  # in_progress, done, failed
    
    # Results
    score: Optional[float] = None
    metadata: Optional[dict[str, Any]] = None
    summary: Optional[str] = None
    error_reason: Optional[str] = None
    
    # Timestamps
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    
    class Config:
        extra = "ignore"


class WinnerInfo(BaseModel):
    """Winner information for reward."""

    winner_approved: bool = False
    winner_hotkey: Optional[str] = None
    winner_agent_id: Optional[str] = None
    winner_score: Optional[float] = None
    
    class Config:
        extra = "ignore"


class UploadToken(BaseModel):
    """Upload token response from /agents/upload/verify."""

    upload_token: str
    tournament_id: str
    expires_at: Optional[datetime] = None
    max_file_size: Optional[int] = None
    
    class Config:
        extra = "ignore"


class ConfigResponse(BaseModel):
    """Configuration file response."""

    config_type: str  # subnet_config, eval_config
    content: dict[str, Any]

