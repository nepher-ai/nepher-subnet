"""Regression tests for the sandbox / weight-setting hardening fixes."""

import importlib.util
import json
import sys
import types
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from nepher_core.api.client import TournamentAPI
from nepher_core.api.exceptions import APIError
from nepher_core.api.models import Agent, Tournament, WinnerInfo
from nepher_core.config import ValidatorConfig
from nepher_core.utils.helpers import unzip_file, zip_directory


def _can_symlink(tmp_path: Path) -> bool:
    try:
        (tmp_path / "_probe").symlink_to(tmp_path)
        (tmp_path / "_probe").unlink()
        return True
    except (OSError, NotImplementedError):
        return False


# ---------------------------------------------------------------------------
# Archive helpers
# ---------------------------------------------------------------------------

class TestArchiveSafety:
    def test_zip_directory_skips_symlinks(self, tmp_path):
        if not _can_symlink(tmp_path):
            pytest.skip("symlinks not permitted on this host")
        secret = tmp_path / "secret.txt"
        secret.write_text("wallet")
        src = tmp_path / "logs"
        src.mkdir()
        (src / "ok.txt").write_text("ok")
        (src / "leak.txt").symlink_to(secret)
        (src / "leakdir").symlink_to(tmp_path)

        archive = tmp_path / "out.zip"
        zip_directory(src, archive)

        names = zipfile.ZipFile(archive).namelist()
        assert names == ["ok.txt"]

    def test_zip_directory_size_cap(self, tmp_path):
        src = tmp_path / "logs"
        src.mkdir()
        (src / "big.bin").write_bytes(b"x" * 2048)
        with pytest.raises(ValueError):
            zip_directory(src, tmp_path / "out.zip", max_total_bytes=1024)

    def test_unzip_rejects_traversal(self, tmp_path):
        archive = tmp_path / "evil.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("../escape.txt", "x")
        with pytest.raises(ValueError, match="Zip slip"):
            unzip_file(archive, tmp_path / "out")

    def test_unzip_rejects_oversized(self, tmp_path):
        archive = tmp_path / "big.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("a.bin", b"x" * 4096)
        with pytest.raises(ValueError, match="exceeds limit"):
            unzip_file(archive, tmp_path / "out", max_total_bytes=1024)

    def test_unzip_rejects_too_many_entries(self, tmp_path):
        archive = tmp_path / "many.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            for i in range(5):
                zf.writestr(f"f{i}.txt", "x")
        with pytest.raises(ValueError, match="too many entries"):
            unzip_file(archive, tmp_path / "out", max_entries=3)


# ---------------------------------------------------------------------------
# Sandbox result collection
# ---------------------------------------------------------------------------

class TestSandboxResult:
    @pytest.fixture
    def runner(self, tmp_path):
        from validator.evaluation.sandbox import SandboxRunner
        return SandboxRunner(workspace=tmp_path, env_cache_path=tmp_path / "cache")

    def _write(self, out: Path, payload) -> None:
        out.mkdir(parents=True, exist_ok=True)
        (out / "evaluation_result.json").write_text(json.dumps(payload))

    def test_sandbox_log_dir_is_ignored(self, runner, tmp_path):
        out = tmp_path / "output"
        self._write(out, {"score": 1.0, "log_dir": "/app/config"})
        result = runner._collect_result(out)
        assert result["log_dir"] is None

    def test_log_dir_set_from_output(self, runner, tmp_path):
        out = tmp_path / "output"
        self._write(out, {"score": 1.0, "log_dir": "/etc"})
        (out / "eval_logs").mkdir()
        result = runner._collect_result(out)
        assert result["log_dir"] == str(out / "eval_logs")

    @pytest.mark.parametrize("score", [None, "1.0", True, float("nan")])
    def test_invalid_score_rejected(self, runner, tmp_path, score):
        from validator.evaluation.sandbox import SandboxError
        out = tmp_path / "output"
        out.mkdir()
        (out / "evaluation_result.json").write_text(
            json.dumps({"score": score}) if score == score else '{"score": NaN}'
        )
        with pytest.raises(SandboxError) as exc:
            runner._collect_result(out)
        assert exc.value.recoverable is False

    def test_symlinked_result_rejected(self, runner, tmp_path):
        if not _can_symlink(tmp_path):
            pytest.skip("symlinks not permitted on this host")
        from validator.evaluation.sandbox import SandboxError
        target = tmp_path / "elsewhere.json"
        target.write_text(json.dumps({"score": 1.0}))
        out = tmp_path / "output"
        out.mkdir()
        (out / "evaluation_result.json").symlink_to(target)
        with pytest.raises(SandboxError):
            runner._collect_result(out)

    @pytest.mark.asyncio
    async def test_infra_error_code_is_recoverable(self, runner, tmp_path):
        from validator.evaluation.sandbox import SandboxError

        async def fake_run(cmd, timeout, log_path):
            out = Path(runner._sandbox_base)
            sandbox_dir = next(out.iterdir())
            self._write(
                sandbox_dir / "output",
                {"score": 0, "metadata": {"error": "gpu_unavailable"}, "summary": "no gpu"},
            )
            return 1

        cfg = tmp_path / "eval_config.yaml"
        cfg.write_text("x: 1")
        with patch.object(runner, "_run_container", side_effect=fake_run), \
             patch.object(runner, "_cleanup_container", new=AsyncMock()):
            with pytest.raises(SandboxError) as exc:
                await runner.run_evaluation(tmp_path / "agent", cfg, "mod")
        assert exc.value.recoverable is True

    @pytest.mark.asyncio
    async def test_agent_error_code_not_recoverable(self, runner, tmp_path):
        from validator.evaluation.sandbox import SandboxError

        async def fake_run(cmd, timeout, log_path):
            sandbox_dir = next(Path(runner._sandbox_base).iterdir())
            self._write(
                sandbox_dir / "output",
                {"score": 0, "metadata": {"error": "install_failed"}, "summary": "bad"},
            )
            return 1

        cfg = tmp_path / "eval_config.yaml"
        cfg.write_text("x: 1")
        with patch.object(runner, "_run_container", side_effect=fake_run), \
             patch.object(runner, "_cleanup_container", new=AsyncMock()):
            with pytest.raises(SandboxError) as exc:
                await runner.run_evaluation(tmp_path / "agent", cfg, "mod")
        assert exc.value.recoverable is False

    def test_read_tail_bounded(self, runner, tmp_path):
        log = tmp_path / "c.log"
        log.write_bytes(b"a" * 50_000 + b"END")
        tail = runner._read_tail(log, max_bytes=100)
        assert len(tail) == 100 and tail.endswith("END")


# ---------------------------------------------------------------------------
# API client: failures must not look like "no tournaments"
# ---------------------------------------------------------------------------

def _api_with(handler) -> TournamentAPI:
    api = TournamentAPI(api_key="k", base_url="https://test.api")
    api._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return api


class TestClientFailures:
    @pytest.mark.asyncio
    async def test_active_list_500_raises(self):
        api = _api_with(lambda req: httpx.Response(500, json={"detail": "boom"}))
        with pytest.raises(APIError):
            await api.get_active_tournaments()

    @pytest.mark.asyncio
    async def test_active_list_404_falls_back_to_single(self):
        def handler(req):
            if req.url.path.endswith("/active/list"):
                return httpx.Response(404, json={"detail": "Not Found"})
            return httpx.Response(404, json={"detail": "none"})
        api = _api_with(handler)
        assert await api.get_active_tournaments() == []

    @pytest.mark.asyncio
    async def test_active_list_empty(self):
        api = _api_with(lambda req: httpx.Response(200, json={"tournaments": []}))
        assert await api.get_active_tournaments() == []

    @pytest.mark.asyncio
    async def test_single_active_500_raises(self):
        api = _api_with(lambda req: httpx.Response(503, json={"detail": "down"}))
        with pytest.raises(APIError):
            await api.get_active_tournament()

    @pytest.mark.asyncio
    async def test_preliminary_leader_error_raises(self):
        api = _api_with(lambda req: httpx.Response(500, json={"detail": "x"}))
        with pytest.raises(APIError):
            await api.get_preliminary_leader("t1")

    @pytest.mark.asyncio
    async def test_preliminary_leader_404_is_empty(self):
        api = _api_with(lambda req: httpx.Response(404, json={"detail": "x"}))
        leader = await api.get_preliminary_leader("t1")
        assert leader.leader_hotkey is None

    @pytest.mark.asyncio
    async def test_download_cap_aborts_and_deletes(self, tmp_path):
        api = _api_with(lambda req: httpx.Response(200, content=b"x" * 5000))
        out = tmp_path / "agent.zip"
        with pytest.raises(ValueError):
            await api.download_agent("a1", out, max_bytes=1000)
        assert not out.exists()

    @pytest.mark.asyncio
    async def test_whitelist_drops_invalid_domains(self):
        items = [
            {"domain": "huggingface.co", "is_active": True},
            {"domain": "evil.com' ; rm -rf / ;'", "is_active": True},
            {"domain": "inactive.com", "is_active": False},
        ]
        api = _api_with(lambda req: httpx.Response(200, json={"items": items}))
        assert await api.get_whitelist_domains() == ["huggingface.co"]

    def test_retry_after_http_date(self):
        from nepher_core.api.exceptions import RateLimitError
        api = TournamentAPI(api_key="k", base_url="https://test.api")
        resp = httpx.Response(
            429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, json={"detail": "slow"}
        )
        with pytest.raises(RateLimitError) as exc:
            api._handle_error_response(resp)
        assert exc.value.retry_after is None


# ---------------------------------------------------------------------------
# Weight setter: lookup failures must not burn
# ---------------------------------------------------------------------------

@pytest.fixture
def weight_setter_module(monkeypatch):
    """Import weight_setter with bittensor stubbed (not installed in CI)."""
    bt = types.ModuleType("bittensor")
    bt.Subtensor = object
    bt.Metagraph = object
    btw = types.ModuleType("bittensor_wallet")
    btw.Wallet = object
    monkeypatch.setitem(sys.modules, "bittensor", bt)
    monkeypatch.setitem(sys.modules, "bittensor_wallet", btw)
    for mod in ("validator.reward.weight_setter", "nepher_core.wallet.utils", "nepher_core.wallet"):
        monkeypatch.delitem(sys.modules, mod, raising=False)
    import validator.reward.weight_setter as ws
    return ws


T0 = 1_700_000_000


def _tournament(tid: str, reward: bool) -> Tournament:
    if reward:
        times = dict(evaluation_end_time=T0 - 300, reward_start_time=T0 - 100, reward_end_time=T0 + 1000)
    else:
        times = dict(evaluation_end_time=T0 + 1000, reward_start_time=T0 + 2000, reward_end_time=T0 + 3000)
    return Tournament(
        id=tid, status="active", contest_start_time=T0 - 1000,
        submit_window_start_time=T0 - 900, contest_end_time=T0 - 800, **times,
    )


class TestWeightSetterFailures:
    def _setter(self, ws, api):
        return ws.WeightSetter(ValidatorConfig(), api)

    def _metagraph(self, hotkeys):
        mg = MagicMock()
        mg.hotkeys = hotkeys
        mg.uids = list(range(len(hotkeys)))
        return mg

    @pytest.mark.asyncio
    async def test_leader_lookup_failure_raises(self, weight_setter_module):
        ws = weight_setter_module
        api = MagicMock()
        api.get_preliminary_leader = AsyncMock(side_effect=APIError("down", status_code=500))
        setter = self._setter(ws, api)
        with patch("validator.reward.weight_setter.get_current_period",
                   return_value=ws.TournamentPeriod.EVALUATION):
            with pytest.raises(ws.WeightLookupError):
                await setter.compute_distribution([_tournament("t1", False)], self._metagraph(["a"]))

    @pytest.mark.asyncio
    async def test_winner_lookup_failure_raises(self, weight_setter_module):
        ws = weight_setter_module
        api = MagicMock()
        api.get_winner_hotkey = AsyncMock(side_effect=APIError("down", status_code=500))
        setter = self._setter(ws, api)
        with patch("validator.reward.weight_setter.get_current_period",
                   return_value=ws.TournamentPeriod.REWARD):
            with pytest.raises(ws.WeightLookupError):
                await setter.compute_distribution([_tournament("t1", True)], self._metagraph(["a"]))

    @pytest.mark.asyncio
    async def test_no_winner_approved_still_burns(self, weight_setter_module):
        ws = weight_setter_module
        api = MagicMock()
        api.get_winner_hotkey = AsyncMock(return_value=WinnerInfo(winner_approved=False))
        setter = self._setter(ws, api)
        with patch("validator.reward.weight_setter.get_current_period",
                   return_value=ws.TournamentPeriod.REWARD):
            dist = await setter.compute_distribution([_tournament("t1", True)], self._metagraph(["a"]))
        assert dist == {0: pytest.approx(1.0)}

    @pytest.mark.asyncio
    async def test_set_combined_weights_does_not_call_chain_on_failure(self, weight_setter_module):
        ws = weight_setter_module
        api = MagicMock()
        api.get_preliminary_leader = AsyncMock(side_effect=APIError("down", status_code=500))
        setter = self._setter(ws, api)
        setter._get_metagraph = AsyncMock(return_value=self._metagraph(["a", "b"]))
        setter._set_weight_distribution = AsyncMock()
        with patch("validator.reward.weight_setter.get_current_period",
                   return_value=ws.TournamentPeriod.EVALUATION):
            with pytest.raises(ws.WeightLookupError):
                await setter.set_combined_weights([_tournament("t1", False)])
        setter._set_weight_distribution.assert_not_called()

    @pytest.mark.asyncio
    async def test_set_weights_runs_off_loop_and_resets_on_error(self, weight_setter_module):
        ws = weight_setter_module
        api = MagicMock()
        api.get_latest_weight_commit = AsyncMock(return_value=None)
        api.report_weight_commit = AsyncMock()
        cfg = ValidatorConfig()
        cfg.retry.weight_setting_max_attempts = 2
        cfg.retry.weight_setting_initial_delay = 1.0
        setter = ws.WeightSetter(cfg, api)
        wallet = MagicMock()
        wallet.hotkey.ss58_address = "5val"
        setter._wallet = wallet

        sub = MagicMock()
        sub.set_weights.side_effect = [RuntimeError("ws closed"), (True, "")]
        setter._subtensor = sub
        with patch("validator.reward.weight_setter.get_subtensor", return_value=sub), \
             patch("asyncio.sleep", new=AsyncMock()):
            await setter._set_weight_distribution({0: 1.0}, self._metagraph(["a", "b"]))
        assert sub.set_weights.call_count == 2
        sub.close.assert_called_once()
        api.report_weight_commit.assert_awaited_once()


# ---------------------------------------------------------------------------
# Orchestrator: infra failures are retried, never scored
# ---------------------------------------------------------------------------

class TestOrchestratorFailures:
    @pytest.fixture
    def orch(self, tmp_path):
        from validator.evaluation.orchestrator import EvaluationOrchestrator
        cfg = ValidatorConfig(paths={"workspace": str(tmp_path), "env_cache": str(tmp_path / "c")})
        api = MagicMock()
        api.submit_failed_evaluation = AsyncMock()
        api.report_agent = AsyncMock()
        o = EvaluationOrchestrator(cfg, api, "5val")
        return o

    @pytest.mark.asyncio
    async def test_recoverable_not_submitted(self, orch):
        from validator.evaluation.agent_evaluator import EvaluationError
        orch.evaluator.evaluate = AsyncMock(side_effect=EvaluationError("docker down", recoverable=True))
        await orch._evaluate_one("t1", Agent(id="a1"))
        orch.api.submit_failed_evaluation.assert_not_called()
        orch.api.report_agent.assert_not_called()
        assert orch._infra_attempts[("t1", "a1")] == 1

    @pytest.mark.asyncio
    async def test_non_recoverable_submitted(self, orch):
        from validator.evaluation.agent_evaluator import EvaluationError
        orch.evaluator.evaluate = AsyncMock(side_effect=EvaluationError("bad zip", recoverable=False))
        await orch._evaluate_one("t1", Agent(id="a1"))
        orch.api.submit_failed_evaluation.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_agent_skipped_after_max_infra_attempts(self, orch):
        from validator.state import TournamentPeriod
        orch._infra_attempts[("t1", "a1")] = orch.config.retry.evaluation_max_attempts
        orch._evaluate_one = AsyncMock()
        orch._fetch_pending = AsyncMock(return_value=[Agent(id="a1")])
        t = _tournament("t1", False)
        with patch("validator.evaluation.orchestrator.get_current_period",
                   return_value=TournamentPeriod.EVALUATION):
            n = await orch.run_evaluation_cycle(
                [t], is_evaluable_fn=AsyncMock(return_value=True),
                prepare_fn=AsyncMock(return_value=True),
            )
        assert n == 0
        orch._evaluate_one.assert_not_called()


# ---------------------------------------------------------------------------
# SNI proxy
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def sni_proxy():
    path = Path(__file__).resolve().parent.parent / "docker" / "sni-proxy.py"
    spec = importlib.util.spec_from_file_location("sni_proxy", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _client_hello(hostname: str) -> bytes:
    """Minimal TLS 1.2 ClientHello carrying one SNI entry."""
    name = hostname.encode()
    sni_entry = b"\x00" + len(name).to_bytes(2, "big") + name
    sni_list = len(sni_entry).to_bytes(2, "big") + sni_entry
    ext = b"\x00\x00" + len(sni_list).to_bytes(2, "big") + sni_list
    body = (
        b"\x03\x03" + b"\x00" * 32 + b"\x00"   # version, random, session_id
        + b"\x00\x02\x13\x01"                  # cipher suites
        + b"\x01\x00"                          # compression
        + len(ext).to_bytes(2, "big") + ext
    )
    hs = b"\x01" + len(body).to_bytes(3, "big") + body
    return b"\x16\x03\x01" + len(hs).to_bytes(2, "big") + hs


class TestSniProxy:
    def test_whitelist_rejects_invalid_hostnames(self, sni_proxy):
        wl = {"huggingface.co"}
        assert sni_proxy.matches_whitelist("cdn.huggingface.co", wl)
        assert not sni_proxy.matches_whitelist("huggingface.co.evil.com", wl)
        assert not sni_proxy.matches_whitelist("1.2.3.4", wl)
        assert not sni_proxy.matches_whitelist("x'y.huggingface.co", wl)

    @pytest.mark.asyncio
    async def test_connects_to_hostname_not_original_ip(self, sni_proxy):
        reader = MagicMock()
        reader.read = AsyncMock(return_value=_client_hello("huggingface.co"))
        writer = MagicMock()
        writer.get_extra_info.side_effect = lambda k: ("10.0.0.5", 1234) if k == "peername" else object()

        server_writer = MagicMock()
        server_writer.drain = AsyncMock()
        open_conn = AsyncMock(return_value=(MagicMock(), server_writer))

        with patch.object(sni_proxy, "get_original_dst", return_value=("6.6.6.6", 443)), \
             patch.object(sni_proxy.asyncio, "open_connection", open_conn), \
             patch.object(sni_proxy, "_relay", new=AsyncMock()):
            await sni_proxy.handle_connection(reader, writer, {"huggingface.co"}, is_tls=True)

        open_conn.assert_awaited_once_with("huggingface.co", 443)

    @pytest.mark.asyncio
    async def test_blocks_non_whitelisted(self, sni_proxy):
        reader = MagicMock()
        reader.read = AsyncMock(return_value=_client_hello("attacker.com"))
        writer = MagicMock()
        writer.get_extra_info.side_effect = lambda k: ("10.0.0.5", 1234) if k == "peername" else object()
        open_conn = AsyncMock()
        with patch.object(sni_proxy, "get_original_dst", return_value=("6.6.6.6", 443)), \
             patch.object(sni_proxy.asyncio, "open_connection", open_conn):
            await sni_proxy.handle_connection(reader, writer, {"huggingface.co"}, is_tls=True)
        open_conn.assert_not_called()
