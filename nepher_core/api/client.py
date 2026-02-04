"""
Tournament API Client.

Unified API client for tournament backend operations.
Used by both miners (submission) and validators (evaluation, settlement).
"""

import asyncio
from pathlib import Path
from typing import Optional, Any
from urllib.parse import urljoin

import httpx
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

from nepher_core.api.models import (
    Tournament,
    Agent,
    AgentListResponse,
    WinnerInfo,
    UploadToken,
)
from nepher_core.api.exceptions import (
    APIError,
    AuthenticationError,
    NotFoundError,
    ValidationError,
    RateLimitError,
)
from nepher_core.utils.logging import get_logger

logger = get_logger(__name__)


class TournamentAPI:
    """
    Unified API client for tournament backend.
    
    Provides methods for:
    - Tournament management (get active, configs)
    - Agent operations (upload, download, list)
    - Evaluation operations (submit, in-progress)
    - Settlement operations (get winner)
    """

    DEFAULT_TIMEOUT = 30.0
    DOWNLOAD_TIMEOUT = 300.0  # 5 minutes for large files
    UPLOAD_TIMEOUT = 600.0  # 10 minutes for uploads

    def __init__(
        self,
        api_key: str,
        base_url: str,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        """
        Initialize the API client.
        
        Args:
            api_key: API key for authentication
            base_url: Base URL of the tournament API
            timeout: Default timeout for requests
        """
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def headers(self) -> dict[str, str]:
        """Default headers for API requests."""
        return {
            "X-API-Key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create async HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout),
                headers=self.headers,
            )
        return self._client

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "TournamentAPI":
        """Async context manager entry."""
        return self

    async def __aexit__(self, *args) -> None:
        """Async context manager exit."""
        await self.close()

    def _build_url(self, path: str) -> str:
        """Build full URL from path."""
        return urljoin(f"{self.base_url}/", path.lstrip("/"))

    def _handle_error_response(self, response: httpx.Response) -> None:
        """Handle error responses and raise appropriate exceptions."""
        status = response.status_code
        try:
            body = response.json()
            message = body.get("detail", body.get("message", str(body)))
        except Exception:
            message = response.text or f"HTTP {status}"

        if status == 401 or status == 403:
            raise AuthenticationError(message, status_code=status)
        elif status == 404:
            raise NotFoundError(message, status_code=status)
        elif status == 400 or status == 422:
            raise ValidationError(message, status_code=status)
        elif status == 429:
            retry_after = response.headers.get("Retry-After")
            raise RateLimitError(
                message,
                status_code=status,
                retry_after=int(retry_after) if retry_after else None,
            )
        else:
            raise APIError(message, status_code=status)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        retry=retry_if_exception_type((httpx.TransportError, RateLimitError)),
    )
    async def _request(
        self,
        method: str,
        path: str,
        **kwargs,
    ) -> httpx.Response:
        """Make an HTTP request with retry logic."""
        client = await self._get_client()
        url = self._build_url(path)
        
        logger.debug(f"API request: {method} {url}")
        response = await client.request(method, url, **kwargs)
        
        if response.status_code >= 400:
            self._handle_error_response(response)
        
        return response

    # =========================================================================
    # Tournament Endpoints
    # =========================================================================

    async def get_active_tournament(self) -> Optional[Tournament]:
        """
        Get the currently active tournament.
        
        Returns:
            Tournament if active, None otherwise
            
        Endpoint: GET /api/v1/tournaments/active
        """
        try:
            response = await self._request("GET", "/api/v1/tournaments/active")
            data = response.json()
            return Tournament(**data) if data else None
        except NotFoundError:
            return None

    async def get_tournament(self, tournament_id: str) -> Tournament:
        """
        Get tournament by ID.
        
        Args:
            tournament_id: Tournament ID
            
        Returns:
            Tournament details
            
        Endpoint: GET /api/v1/tournaments/{id}
        """
        response = await self._request("GET", f"/api/v1/tournaments/{tournament_id}")
        return Tournament(**response.json())

    async def get_subnet_config(self, tournament_id: str) -> dict[str, Any]:
        """
        Download subnet configuration for a tournament.
        
        Args:
            tournament_id: Tournament ID
            
        Returns:
            Subnet configuration dict
            
        Endpoint: GET /api/v1/tournaments/{id}/config/subnet_config
        """
        response = await self._request(
            "GET",
            f"/api/v1/tournaments/{tournament_id}/config/subnet_config",
        )
        return response.json()

    async def get_task_config(self, tournament_id: str) -> dict[str, Any]:
        """
        Download task/evaluation configuration for a tournament.
        
        Args:
            tournament_id: Tournament ID
            
        Returns:
            Task configuration dict
            
        Endpoint: GET /api/v1/tournaments/{id}/config/eval_config
        """
        response = await self._request(
            "GET",
            f"/api/v1/tournaments/{tournament_id}/config/eval_config",
        )
        return response.json()

    async def get_winner_hotkey(self, tournament_id: str) -> WinnerInfo:
        """
        Get winner information for settlement.
        
        Args:
            tournament_id: Tournament ID
            
        Returns:
            Winner information including hotkey
            
        Endpoint: GET /api/v1/tournaments/{id}/winner-hotkey
        """
        response = await self._request(
            "GET",
            f"/api/v1/tournaments/{tournament_id}/winner-hotkey",
        )
        return WinnerInfo(**response.json())

    # =========================================================================
    # Agent Endpoints
    # =========================================================================

    async def request_upload_token(
        self,
        tournament_id: str,
        miner_hotkey: str,
        signature: str,
        file_checksum: str,
        file_size: int,
        agent_name: Optional[str] = None,
    ) -> UploadToken:
        """
        Request an upload token for agent submission.
        
        Args:
            tournament_id: Tournament ID
            miner_hotkey: Miner's hotkey
            signature: Signature proving hotkey ownership
            file_checksum: SHA256 checksum of the file
            file_size: Size of the file in bytes
            agent_name: Optional agent name
            
        Returns:
            Upload token with URL
            
        Endpoint: POST /api/v1/agents/upload/verify
        """
        response = await self._request(
            "POST",
            "/api/v1/agents/upload/verify",
            json={
                "tournament_id": tournament_id,
                "miner_hotkey": miner_hotkey,
                "signature": signature,
                "file_checksum": file_checksum,
                "file_size": file_size,
                "agent_name": agent_name,
            },
        )
        return UploadToken(**response.json())

    async def upload_agent(
        self,
        agent_id: str,
        file_path: Path,
    ) -> Agent:
        """
        Upload agent file.
        
        Args:
            agent_id: Agent ID from upload token
            file_path: Path to the ZIP file
            
        Returns:
            Created agent
            
        Endpoint: POST /api/v1/agents/upload/{id}
        """
        client = await self._get_client()
        url = self._build_url(f"/api/v1/agents/upload/{agent_id}")
        
        with open(file_path, "rb") as f:
            files = {"file": (file_path.name, f, "application/zip")}
            response = await client.post(
                url,
                files=files,
                timeout=httpx.Timeout(self.UPLOAD_TIMEOUT),
            )
        
        if response.status_code >= 400:
            self._handle_error_response(response)
        
        return Agent(**response.json())

    async def get_pending_agents(
        self,
        tournament_id: str,
        validator_hotkey: str,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> AgentListResponse:
        """
        Get agents not yet evaluated by this validator.
        
        Args:
            tournament_id: Tournament ID
            validator_hotkey: Validator's hotkey
            limit: Pagination limit
            offset: Pagination offset
            
        Returns:
            List of unevaluated agents
            
        Endpoint: GET /api/v1/agents/list/unevaluated
        """
        params = {
            "tournament_id": tournament_id,
            "validator_hotkey": validator_hotkey,
        }
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset

        response = await self._request(
            "GET",
            "/api/v1/agents/list/unevaluated",
            params=params,
        )
        return AgentListResponse(**response.json())

    async def download_agent(
        self,
        agent_id: str,
        output_path: Path,
    ) -> Path:
        """
        Download agent ZIP file.
        
        Args:
            agent_id: Agent ID
            output_path: Path to save the file
            
        Returns:
            Path to downloaded file
            
        Endpoint: GET /api/v1/agents/download/{id}
        """
        client = await self._get_client()
        url = self._build_url(f"/api/v1/agents/download/{agent_id}")
        
        async with client.stream(
            "GET",
            url,
            timeout=httpx.Timeout(self.DOWNLOAD_TIMEOUT),
        ) as response:
            if response.status_code >= 400:
                await response.aread()
                self._handle_error_response(response)
            
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "wb") as f:
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    f.write(chunk)
        
        logger.info(f"Downloaded agent to {output_path}")
        return output_path

    # =========================================================================
    # Evaluation Endpoints
    # =========================================================================

    async def set_evaluation_in_progress(
        self,
        tournament_id: str,
        agent_id: str,
        validator_hotkey: str,
    ) -> None:
        """
        Mark evaluation as in-progress.
        
        Args:
            tournament_id: Tournament ID
            agent_id: Agent ID being evaluated
            validator_hotkey: Validator's hotkey
            
        Endpoint: POST /api/v1/evaluations/in-progress
        """
        await self._request(
            "POST",
            "/api/v1/evaluations/in-progress",
            json={
                "tournament_id": tournament_id,
                "agent_id": agent_id,
                "validator_hotkey": validator_hotkey,
            },
        )
        logger.info(f"Marked evaluation in-progress: agent={agent_id}")

    async def clear_evaluation_in_progress(
        self,
        tournament_id: str,
        validator_hotkey: str,
    ) -> None:
        """
        Clear in-progress status for this validator.
        
        Args:
            tournament_id: Tournament ID
            validator_hotkey: Validator's hotkey
            
        Endpoint: POST /api/v1/evaluations/in-progress (with agent_id=null)
        """
        await self._request(
            "POST",
            "/api/v1/evaluations/in-progress",
            json={
                "tournament_id": tournament_id,
                "agent_id": None,
                "validator_hotkey": validator_hotkey,
            },
        )
        logger.debug(f"Cleared in-progress status for validator={validator_hotkey}")

    async def submit_evaluation(
        self,
        tournament_id: str,
        agent_id: str,
        validator_hotkey: str,
        score: float,
        metadata: dict[str, Any],
        summary: str,
        log_file: Optional[Path] = None,
    ) -> None:
        """
        Submit successful evaluation result.
        
        Args:
            tournament_id: Tournament ID
            agent_id: Agent ID
            validator_hotkey: Validator's hotkey
            score: Evaluation score
            metadata: Evaluation metadata
            summary: Human-readable summary
            log_file: Optional path to logs ZIP
            
        Endpoint: POST /api/v1/evaluations/submit
        """
        data = {
            "tournament_id": tournament_id,
            "agent_id": agent_id,
            "validator_hotkey": validator_hotkey,
            "status": "done",
            "score": score,
            "metadata": metadata,
            "summary": summary,
        }

        if log_file and log_file.exists():
            client = await self._get_client()
            url = self._build_url("/api/v1/evaluations/submit")
            
            with open(log_file, "rb") as f:
                files = {"log_file": (log_file.name, f, "application/zip")}
                response = await client.post(
                    url,
                    data={"data": str(data)},
                    files=files,
                    timeout=httpx.Timeout(self.UPLOAD_TIMEOUT),
                )
            
            if response.status_code >= 400:
                self._handle_error_response(response)
        else:
            await self._request("POST", "/api/v1/evaluations/submit", json=data)

        logger.info(f"Submitted evaluation: agent={agent_id}, score={score}")

    async def submit_failed_evaluation(
        self,
        tournament_id: str,
        agent_id: str,
        validator_hotkey: str,
        error_reason: str,
    ) -> None:
        """
        Submit failed evaluation result.
        
        Args:
            tournament_id: Tournament ID
            agent_id: Agent ID
            validator_hotkey: Validator's hotkey
            error_reason: Reason for failure
            
        Endpoint: POST /api/v1/evaluations/submit (status=failed)
        """
        await self._request(
            "POST",
            "/api/v1/evaluations/submit",
            json={
                "tournament_id": tournament_id,
                "agent_id": agent_id,
                "validator_hotkey": validator_hotkey,
                "status": "failed",
                "error_reason": error_reason,
            },
        )
        logger.warning(f"Submitted failed evaluation: agent={agent_id}, reason={error_reason}")

