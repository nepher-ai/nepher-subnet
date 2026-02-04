"""API response models."""

from datetime import datetime
from typing import Optional, List, Any
from pydantic import BaseModel, Field


class Tournament(BaseModel):
    """Tournament model."""

    id: str
    name: str
    status: str  # pending, active, review, settlement, done, cancelled
    network: str = "finney"
    subnet_uid: int = 49

    # Timestamps (Unix timestamps)
    contest_start_time: int
    grace_window_start_time: int
    contest_end_time: int
    evaluation_end_time: int
    settlement_end_time: int

    # Optional fields
    description: Optional[str] = None
    task_name: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class Agent(BaseModel):
    """Agent submission model."""

    id: str
    tournament_id: str
    miner_hotkey: str
    status: str  # pending, evaluating, evaluated, failed
    
    # Optional fields
    name: Optional[str] = None
    file_path: Optional[str] = None
    file_size: Optional[int] = None
    checksum: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class AgentListResponse(BaseModel):
    """Response for agent list endpoints."""

    agents: List[Agent]
    total: int
    limit: int = 0
    offset: int = 0


class Evaluation(BaseModel):
    """Evaluation result model."""

    id: str
    agent_id: str
    tournament_id: str
    validator_hotkey: str
    status: str  # in_progress, done, failed
    
    # Results
    score: Optional[float] = None
    metadata: Optional[dict[str, Any]] = None
    summary: Optional[str] = None
    error_reason: Optional[str] = None
    
    # Timestamps
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class WinnerInfo(BaseModel):
    """Winner information for settlement."""

    winner_approved: bool = False
    winner_hotkey: Optional[str] = None
    winner_agent_id: Optional[str] = None
    winner_score: Optional[float] = None


class UploadToken(BaseModel):
    """Upload token response."""

    agent_id: str
    upload_url: str
    expires_at: datetime


class ConfigResponse(BaseModel):
    """Configuration file response."""

    config_type: str  # subnet_config, eval_config
    content: dict[str, Any]

