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
    submit_window_start_time: Optional[int] = None
    contest_end_time: Optional[int] = None
    evaluation_start_time: Optional[int] = None
    evaluation_end_time: Optional[int] = None
    reward_start_time: Optional[int] = None
    reward_end_time: Optional[int] = None

    # Two-phase evaluation
    has_public_eval: bool = False
    current_eval_phase: Optional[str] = None  # "public", "private", "quiet_zone", None
    public_eval_end_time: Optional[int] = None
    public_eval_buffer_hours: Optional[int] = None

    # Tournament format (rl, vla, wam, lwm, task_generation) and where weights live.
    # "inline" tournaments upload everything; "huggingface" ones seal weights on HF.
    format: Optional[str] = None
    submission_mode: str = "inline"
    base_weight_sha256: List[str] = Field(default_factory=list)

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

    # Set only for huggingface tournaments (sealed, archiving, archived, disqualified, ...).
    custody_status: Optional[str] = None
    hf_revision: Optional[str] = None
    weights_fingerprint: Optional[str] = None

    class Config:
        extra = "ignore"


class AgentCustody(BaseModel):
    """Current custody state of one agent (GET /agents/{id}/custody)."""

    agent_id: str
    custody_status: str
    weights_fingerprint: Optional[str] = None
    hf_revision: Optional[str] = None
    checked_at: Optional[datetime] = None
    reason: Optional[str] = None

    class Config:
        extra = "ignore"


class WeightLeaseFile(BaseModel):
    """One sealed weight file and its short-lived download URL."""

    path: str
    sha256: str
    size: int
    url: str
    url_expires_at: Optional[datetime] = None

    class Config:
        extra = "ignore"


class WeightLease(BaseModel):
    """Presigned download URLs for the sealed weights of one agent."""

    agent_id: str
    revision: str
    expires_at: Optional[datetime] = None
    files: List[WeightLeaseFile] = Field(default_factory=list)

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
    phase: str = "private"  # "public" or "private"
    
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


class PodiumWinnerInfo(BaseModel):
    """One podium place returned by the tournament reward API."""

    place: int = 1
    hotkey: Optional[str] = None
    agent_id: Optional[str] = None
    score: Optional[float] = None
    reward_share: float = 0.0
    # Present for huggingface tournaments; payout requires "archived".
    custody_status: Optional[str] = None

    class Config:
        extra = "ignore"


class WinnerInfo(BaseModel):
    """Winner information for reward.

    Singular fields are place-1 mirrors; ``winners`` carries the full podium
    with normalized ``reward_share`` fractions of the remainder weight.
    """

    winner_approved: bool = False
    winner_hotkey: Optional[str] = None
    winner_agent_id: Optional[str] = None
    winner_score: Optional[float] = None
    winner_custody_status: Optional[str] = None
    reward_split_ratios: Optional[List[int]] = None
    winners: List[PodiumWinnerInfo] = Field(default_factory=list)

    class Config:
        extra = "ignore"


class PreliminaryLeaderInfo(BaseModel):
    """Current leaderboard leader for pre-reward emission allocation."""

    leader_hotkey: Optional[str] = None
    leader_agent_id: Optional[str] = None
    leader_score: Optional[float] = None
    phase: str = "public"

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


class EvaluationToken(BaseModel):
    """Evaluation upload token response from /evaluations/submit/verify."""

    upload_token: str
    tournament_id: str
    agent_id: str
    phase: str = "private"
    public_eval_end_time: Optional[int] = None
    expires_at: Optional[datetime] = None
    max_log_file_size: Optional[int] = None
    existing_status: Optional[str] = None

    class Config:
        extra = "ignore"


class WeightCommitInfo(BaseModel):
    """Latest weight commit from the backend for dedup coordination."""

    weight_hash: str
    weight_data: dict[str, float]
    committed_at: datetime

    class Config:
        extra = "ignore"


class ConfigResponse(BaseModel):
    """Configuration file response."""

    config_type: str  # subnet_config, eval_config
    content: dict[str, Any]

