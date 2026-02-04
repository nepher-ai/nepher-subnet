"""Tests for the Tournament API client."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

from nepher_core.api.client import TournamentAPI
from nepher_core.api.models import Tournament, Agent, AgentListResponse, WinnerInfo
from nepher_core.api.exceptions import (
    APIError,
    AuthenticationError,
    NotFoundError,
)


@pytest.fixture
def api():
    """Create API client fixture."""
    return TournamentAPI(
        api_key="test_api_key",
        base_url="https://test.api.com",
    )


class TestTournamentAPI:
    """Test cases for TournamentAPI."""

    def test_init(self, api):
        """Test API client initialization."""
        assert api.api_key == "test_api_key"
        assert api.base_url == "https://test.api.com"
        assert "X-API-Key" in api.headers
        assert api.headers["X-API-Key"] == "test_api_key"

    def test_build_url(self, api):
        """Test URL building."""
        assert api._build_url("/api/v1/test") == "https://test.api.com/api/v1/test"
        assert api._build_url("api/v1/test") == "https://test.api.com/api/v1/test"

    @pytest.mark.asyncio
    async def test_get_active_tournament_success(self, api):
        """Test getting active tournament."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "id": "test-tournament",
            "name": "Test Tournament",
            "status": "active",
            "network": "finney",
            "subnet_uid": 49,
            "contest_start_time": 1000000,
            "grace_window_start_time": 1100000,
            "contest_end_time": 1200000,
            "evaluation_end_time": 1300000,
            "settlement_end_time": 1400000,
        }

        with patch.object(api, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = mock_response
            
            tournament = await api.get_active_tournament()
            
            assert tournament is not None
            assert tournament.id == "test-tournament"
            assert tournament.name == "Test Tournament"
            assert tournament.status == "active"

    @pytest.mark.asyncio
    async def test_get_active_tournament_none(self, api):
        """Test getting active tournament when none exists."""
        with patch.object(api, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.side_effect = NotFoundError("No active tournament")
            
            tournament = await api.get_active_tournament()
            
            assert tournament is None

    @pytest.mark.asyncio
    async def test_get_pending_agents(self, api):
        """Test getting pending agents."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "agents": [
                {
                    "id": "agent-1",
                    "tournament_id": "test-tournament",
                    "miner_hotkey": "5xxx",
                    "status": "pending",
                }
            ],
            "total": 1,
            "limit": 10,
            "offset": 0,
        }

        with patch.object(api, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = mock_response
            
            response = await api.get_pending_agents(
                tournament_id="test-tournament",
                validator_hotkey="5yyy",
            )
            
            assert response.total == 1
            assert len(response.agents) == 1
            assert response.agents[0].id == "agent-1"

    @pytest.mark.asyncio
    async def test_get_winner_hotkey(self, api):
        """Test getting winner hotkey."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "winner_approved": True,
            "winner_hotkey": "5xxx",
            "winner_agent_id": "agent-1",
            "winner_score": 95.5,
        }

        with patch.object(api, "_request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = mock_response
            
            winner = await api.get_winner_hotkey("test-tournament")
            
            assert winner.winner_approved is True
            assert winner.winner_hotkey == "5xxx"
            assert winner.winner_score == 95.5


class TestErrorHandling:
    """Test error handling."""

    def test_authentication_error(self, api):
        """Test authentication error handling."""
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.json.return_value = {"detail": "Invalid API key"}
        mock_response.text = "Invalid API key"

        with pytest.raises(AuthenticationError) as exc_info:
            api._handle_error_response(mock_response)
        
        assert exc_info.value.status_code == 401

    def test_not_found_error(self, api):
        """Test not found error handling."""
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.json.return_value = {"detail": "Not found"}
        mock_response.text = "Not found"

        with pytest.raises(NotFoundError) as exc_info:
            api._handle_error_response(mock_response)
        
        assert exc_info.value.status_code == 404

