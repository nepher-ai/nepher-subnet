"""
Tournament API Client.

Unified API client for tournament backend operations.
Used by both miners (submission) and validators (evaluation, reward).
"""

import asyncio
from pathlib import Path
from typing import Optional, Any
from urllib.parse import urljoin

import httpx
import yaml
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
    EvaluationToken,
)
from nepher_core.api.exceptions import (
    APIError,
    AuthenticationError,
    NotFoundError,
    QuietZoneError,
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
    - Reward operations (get winner)
    """

    DEFAULT_TIMEOUT = 30.0
    DOWNLOAD_TIMEOUT = 300.0  # 5 minutes for large files
    UPLOAD_TIMEOUT = 600.0  # 10 minutes for uploads

    def __init__(
        self,
        api_key: str,
        base_url: str,
        timeout: float = DEFAULT_TIMEOUT,
        wallet: Optional[Any] = None,
    ):
        """
        Initialize the API client.
        
        Args:
            api_key: API key for authentication
            base_url: Base URL of the tournament API
            timeout: Default timeout for requests
            wallet: Optional Bittensor wallet for signing evaluation requests.
                    Required for evaluation operations (in-progress, submit).
        """
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.wallet = wallet
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
        raw_body = response.text
        
        # Always log the raw response for debugging
        logger.error(f"API error {status}, raw response: {raw_body}")
        
        try:
            body = response.json()
            # Handle FastAPI validation errors which have 'detail' as a list
            # Also handle API format with 'details' (plural)
            detail = body.get("detail") or body.get("details")
            if isinstance(detail, list):
                # Format validation errors nicely
                errors = []
                for err in detail:
                    if isinstance(err, dict):
                        loc = err.get("loc", [])
                        msg = err.get("msg", "")
                        loc_str = " -> ".join(str(x) for x in loc)
                        errors.append(f"  {loc_str}: {msg}")
                    else:
                        errors.append(f"  {err}")
                message = "Validation errors:\n" + "\n".join(errors)
            elif isinstance(detail, str):
                message = detail
            elif detail is not None:
                message = str(detail)
            else:
                message = body.get("message", str(body))
        except Exception as e:
            logger.debug(f"Failed to parse error response: {e}")
            message = raw_body or f"HTTP {status}"

        if status == 401 or status == 403:
            raise AuthenticationError(message, status_code=status, response_body=raw_body)
        elif status == 404:
            raise NotFoundError(message, status_code=status, response_body=raw_body)
        elif status == 400 or status == 422:
            raise ValidationError(message, status_code=status, response_body=raw_body)
        elif status == 409:
            raise QuietZoneError(message, status_code=status, response_body=raw_body)
        elif status == 429:
            retry_after = response.headers.get("Retry-After")
            raise RateLimitError(
                message,
                status_code=status,
                retry_after=int(retry_after) if retry_after else None,
            )
        else:
            raise APIError(message, status_code=status, response_body=raw_body)

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
        """Make an HTTP request with retry logic.
        
        Args:
            method: HTTP method
            path: API path
            expect_json: If True (default), verify the response has a JSON
                content-type and raise APIError otherwise. Set to False for
                endpoints that return non-JSON payloads (e.g. YAML configs).
            **kwargs: Extra arguments forwarded to httpx.
        """
        # Pop our custom flag before forwarding kwargs to httpx
        expect_json = kwargs.pop("expect_json", True)

        client = await self._get_client()
        url = self._build_url(path)
        
        logger.debug(f"API request: {method} {url}")
        response = await client.request(method, url, **kwargs)
        
        if response.status_code >= 400:
            self._handle_error_response(response)
        
        # Verify the response is JSON when we expect it to be.
        # A common misconfiguration is pointing at a frontend SPA which
        # returns 200 + HTML for every route.
        if expect_json:
            content_type = response.headers.get("content-type", "")
            if response.status_code == 200 and "application/json" not in content_type:
                body_preview = response.text[:200] if response.text else "(empty)"
                raise APIError(
                    f"Expected JSON response from {method} {url} but got "
                    f"content-type '{content_type}'. This usually means "
                    f"NEPHER_API_URL ({self.base_url}) is pointing at the "
                    f"frontend app instead of the API backend. "
                    f"Response preview: {body_preview}",
                    status_code=response.status_code,
                    response_body=response.text,
                )
        
        return response

    # =========================================================================
    # Tournament Endpoints
    # =========================================================================

    async def get_active_tournament(self) -> Optional[Tournament]:
        """
        Get the currently active tournament.

        Passes ``subnet=true`` so the backend returns a minimized payload
        (no YAML configs, no statistics, no block numbers) that contains
        only the fields required by subnet validators.
        
        Returns:
            Tournament if active, None otherwise
            
        Endpoint: GET /api/v1/tournaments/active?subnet=true
        """
        try:
            logger.info(f"Fetching active tournament from {self.base_url}/api/v1/tournaments/active")
            response = await self._request(
                "GET",
                "/api/v1/tournaments/active",
                params={"subnet": "true"},
            )
            data = response.json()
            if data:
                tournament = Tournament(**data)
                logger.info(
                    f"Active tournament found: {tournament.id} "
                    f"(status={tournament.status}, name={tournament.name})"
                )
                return tournament
            else:
                logger.info("API returned empty response — no active tournament")
                return None
        except NotFoundError:
            logger.info("No active tournament (404)")
            return None
        except APIError as e:
            logger.error(f"Failed to fetch active tournament: {e}")
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
            expect_json=False,
        )
        content_type = response.headers.get("content-type", "")
        if "yaml" in content_type or "x-yaml" in content_type:
            return yaml.safe_load(response.text)
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
            expect_json=False,
        )
        content_type = response.headers.get("content-type", "")
        if "yaml" in content_type or "x-yaml" in content_type:
            return yaml.safe_load(response.text)
        return response.json()

    async def get_active_eval_config(self, tournament_id: str) -> tuple[str, dict[str, Any]]:
        """
        Download the phase-appropriate eval config.

        During public evaluation the backend serves public_eval_config_yaml;
        during quiet zone / private evaluation it serves eval_config_yaml.

        Args:
            tournament_id: Tournament ID

        Returns:
            (phase, config_dict) where phase comes from the X-Eval-Phase header.

        Endpoint: GET /api/v1/tournaments/{id}/config/active_eval_config
        """
        response = await self._request(
            "GET",
            f"/api/v1/tournaments/{tournament_id}/config/active_eval_config",
            expect_json=False,
        )
        phase = response.headers.get("x-eval-phase", "private")
        content_type = response.headers.get("content-type", "")
        if "yaml" in content_type or "x-yaml" in content_type:
            return phase, yaml.safe_load(response.text)
        return phase, response.json()

    async def get_winner_hotkey(self, tournament_id: str) -> WinnerInfo:
        """
        Get winner information for reward.
        
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
        miner_hotkey: str,
        public_key: str,
        file_info: str,
        signature: str,
        file_size: int,
    ) -> UploadToken:
        """
        Request an upload token for agent submission.
        
        Args:
            miner_hotkey: Miner's SS58 hotkey address
            public_key: Hex-encoded public key of the hotkey
            file_info: Signed message in format "hotkey:content_hash:timestamp"
            signature: Hex-encoded signature of file_info
            file_size: Size of the file in bytes
            
        Returns:
            Upload token with tournament_id
            
        Endpoint: POST /api/v1/agents/upload/verify
        """
        response = await self._request(
            "POST",
            "/api/v1/agents/upload/verify",
            json={
                "miner_hotkey": miner_hotkey,
                "public_key": public_key,
                "file_info": file_info,
                "signature": signature,
                "file_size": file_size,
            },
        )
        return UploadToken(**response.json())

    async def upload_agent(
        self,
        tournament_id: str,
        upload_token: str,
        miner_hotkey: str,
        content_hash: str,
        file_path: Path,
    ) -> Agent:
        """
        Upload agent file using upload token.
        
        Args:
            tournament_id: Tournament ID from verify response
            upload_token: Upload token from verify response
            miner_hotkey: Miner's SS58 hotkey address
            content_hash: SHA256 checksum of the file
            file_path: Path to the ZIP file
            
        Returns:
            Created agent
            
        Endpoint: POST /api/v1/agents/upload/{tournament_id}
        """
        url = self._build_url(f"/api/v1/agents/upload/{tournament_id}")
        
        # Use a separate client for file uploads to avoid Content-Type conflicts
        # The default client has Content-Type: application/json which breaks multipart uploads
        upload_headers = {
            "X-API-Key": self.api_key,
            "Accept": "application/json",
            "X-Upload-Token": upload_token,
        }
        
        async with httpx.AsyncClient(timeout=httpx.Timeout(self.UPLOAD_TIMEOUT)) as upload_client:
            with open(file_path, "rb") as f:
                files = {"file": (file_path.name, f, "application/zip")}
                data = {
                    "miner_hotkey": miner_hotkey,
                    "content_hash": content_hash,
                }
                response = await upload_client.post(
                    url,
                    files=files,
                    data=data,
                    headers=upload_headers,
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
        phase: Optional[str] = None,
    ) -> AgentListResponse:
        """
        Get agents not yet evaluated by this validator.
        
        Args:
            tournament_id: Tournament ID
            validator_hotkey: Validator's hotkey
            limit: Pagination limit
            offset: Pagination offset
            phase: Evaluation phase ('public' or 'private'). When set, only
                   evaluations in that phase count as completed.
            
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
        if phase is not None:
            params["phase"] = phase

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
    # Evaluation Endpoints (CLI two-step verify → submit with wallet signature)
    # =========================================================================

    def _require_wallet(self) -> None:
        """Raise if wallet is not configured."""
        if self.wallet is None:
            raise APIError(
                "Wallet is required for evaluation operations. "
                "Pass wallet= when constructing TournamentAPI.",
                status_code=0,
            )

    def _sign_eval_info(self, eval_info: str) -> tuple[str, str]:
        """
        Sign an eval_info string using the wallet.

        Returns:
            (public_key_hex, signature_hex)
        """
        from nepher_core.wallet import get_public_key, sign_message

        public_key = get_public_key(self.wallet)
        signature = sign_message(self.wallet, eval_info)
        return public_key, signature

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

        Endpoint: POST /api/v1/evaluations/in-progress/cli
        """
        self._require_wallet()

        from nepher_core.wallet import create_eval_info

        eval_info = create_eval_info(
            validator_hotkey=validator_hotkey,
            tournament_id=tournament_id,
            agent_id=agent_id,
        )
        public_key, signature = self._sign_eval_info(eval_info)

        await self._request(
            "POST",
            "/api/v1/evaluations/in-progress/cli",
            json={
                "tournament_id": tournament_id,
                "agent_id": agent_id,
                "validator_hotkey": validator_hotkey,
                "public_key": public_key,
                "eval_info": eval_info,
                "signature": signature,
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

        Endpoint: POST /api/v1/evaluations/in-progress/cli (with agent_id=null)
        """
        self._require_wallet()

        from nepher_core.wallet import create_eval_info

        eval_info = create_eval_info(
            validator_hotkey=validator_hotkey,
            tournament_id=tournament_id,
            agent_id=None,
        )
        public_key, signature = self._sign_eval_info(eval_info)

        await self._request(
            "POST",
            "/api/v1/evaluations/in-progress/cli",
            json={
                "tournament_id": tournament_id,
                "agent_id": None,
                "validator_hotkey": validator_hotkey,
                "public_key": public_key,
                "eval_info": eval_info,
                "signature": signature,
            },
        )
        logger.debug(f"Cleared in-progress status for validator={validator_hotkey}")

    async def _verify_evaluation_submit(
        self,
        tournament_id: str,
        agent_id: str,
        validator_hotkey: str,
        log_file_size: Optional[int] = None,
        log_hash: Optional[str] = None,
    ) -> EvaluationToken:
        """
        Step 1 of evaluation submission: verify and obtain upload token.

        Args:
            tournament_id: Tournament ID
            agent_id: Agent ID
            validator_hotkey: Validator's hotkey
            log_file_size: Size of log file in bytes (optional)
            log_hash: SHA256 hash of log file (optional)

        Returns:
            EvaluationToken with upload_token

        Endpoint: POST /api/v1/evaluations/submit/verify
        """
        self._require_wallet()

        from nepher_core.wallet import create_eval_info

        eval_info = create_eval_info(
            validator_hotkey=validator_hotkey,
            tournament_id=tournament_id,
            agent_id=agent_id,
            log_hash=log_hash,
        )
        public_key, signature = self._sign_eval_info(eval_info)

        body: dict[str, Any] = {
            "validator_hotkey": validator_hotkey,
            "public_key": public_key,
            "eval_info": eval_info,
            "signature": signature,
            "tournament_id": tournament_id,
            "agent_id": agent_id,
        }
        if log_file_size is not None:
            body["log_file_size"] = log_file_size

        response = await self._request(
            "POST",
            "/api/v1/evaluations/submit/verify",
            json=body,
        )
        return EvaluationToken(**response.json())

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
        Submit successful evaluation result (two-step verify → submit).

        Args:
            tournament_id: Tournament ID
            agent_id: Agent ID
            validator_hotkey: Validator's hotkey
            score: Evaluation score
            metadata: Evaluation metadata
            summary: Human-readable summary
            log_file: Optional path to logs ZIP

        Endpoint:
            1. POST /api/v1/evaluations/submit/verify
            2. POST /api/v1/evaluations/submit (Form + X-Upload-Token)
        """
        import hashlib
        import json

        # Calculate log hash if log file exists
        log_hash: Optional[str] = None
        log_file_size: Optional[int] = None
        if log_file and log_file.exists():
            content = log_file.read_bytes()
            log_hash = hashlib.sha256(content).hexdigest()
            log_file_size = len(content)

        # Step 1: Verify and get upload token
        token = await self._verify_evaluation_submit(
            tournament_id=tournament_id,
            agent_id=agent_id,
            validator_hotkey=validator_hotkey,
            log_file_size=log_file_size,
            log_hash=log_hash,
        )

        # Step 2: Submit with Form data + upload token
        url = self._build_url("/api/v1/evaluations/submit")
        upload_headers = {
            "X-API-Key": self.api_key,
            "Accept": "application/json",
            "X-Upload-Token": token.upload_token,
        }

        form_data = {
            "validator_hotkey": validator_hotkey,
            "score": str(score),
            "metadata": json.dumps(metadata) if metadata else None,
            "summary": summary or None,
        }
        if log_hash:
            form_data["log_hash"] = log_hash
        # Remove None values
        form_data = {k: v for k, v in form_data.items() if v is not None}

        async with httpx.AsyncClient(timeout=httpx.Timeout(self.UPLOAD_TIMEOUT)) as upload_client:
            if log_file and log_file.exists():
                with open(log_file, "rb") as f:
                    files = {"log_file": (log_file.name, f, "application/zip")}
                    response = await upload_client.post(
                        url,
                        data=form_data,
                        files=files,
                        headers=upload_headers,
                    )
            else:
                response = await upload_client.post(
                    url,
                    data=form_data,
                    headers=upload_headers,
                )

        if response.status_code >= 400:
            self._handle_error_response(response)

        logger.info(f"Submitted evaluation: agent={agent_id}, score={score}")

    async def submit_failed_evaluation(
        self,
        tournament_id: str,
        agent_id: str,
        validator_hotkey: str,
        error_reason: str,
    ) -> None:
        """
        Submit failed evaluation result (two-step verify → submit).

        The backend /submit endpoint marks status as "done" when a score is
        provided. For failures we submit score=0 with error info in summary
        so the backend records the evaluation.

        Args:
            tournament_id: Tournament ID
            agent_id: Agent ID
            validator_hotkey: Validator's hotkey
            error_reason: Reason for failure

        Endpoint:
            1. POST /api/v1/evaluations/submit/verify
            2. POST /api/v1/evaluations/submit (Form + X-Upload-Token)
        """
        # Step 1: Verify and get upload token
        token = await self._verify_evaluation_submit(
            tournament_id=tournament_id,
            agent_id=agent_id,
            validator_hotkey=validator_hotkey,
        )

        # Step 2: Submit with Form data + upload token (score=0, error in summary)
        url = self._build_url("/api/v1/evaluations/submit")
        upload_headers = {
            "X-API-Key": self.api_key,
            "Accept": "application/json",
            "X-Upload-Token": token.upload_token,
        }

        form_data = {
            "validator_hotkey": validator_hotkey,
            "score": "0",
            "summary": f"[FAILED] {error_reason}",
        }

        async with httpx.AsyncClient(timeout=httpx.Timeout(self.UPLOAD_TIMEOUT)) as upload_client:
            response = await upload_client.post(
                url,
                data=form_data,
                headers=upload_headers,
            )

        if response.status_code >= 400:
            self._handle_error_response(response)

        logger.warning(f"Submitted failed evaluation: agent={agent_id}, reason={error_reason}")

