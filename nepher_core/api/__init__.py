"""Tournament API client module."""

from nepher_core.api.client import TournamentAPI
from nepher_core.api.models import (
    Tournament,
    Agent,
    Evaluation,
    WinnerInfo,
    AgentListResponse,
)
from nepher_core.api.exceptions import (
    APIError,
    AuthenticationError,
    NotFoundError,
    ValidationError,
    RateLimitError,
)

__all__ = [
    "TournamentAPI",
    "Tournament",
    "Agent",
    "Evaluation",
    "WinnerInfo",
    "AgentListResponse",
    "APIError",
    "AuthenticationError",
    "NotFoundError",
    "ValidationError",
    "RateLimitError",
]

