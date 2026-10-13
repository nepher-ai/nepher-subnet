"""Hugging Face weight custody: definitions, download, evaluator gate, and reward gate."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import yaml

from miner.brain_layout import validate_brain_agent_structure
from nepher_core.api.client import TournamentAPI
from nepher_core.api.exceptions import APIError
from nepher_core.api.models import (
    Agent,
    AgentCustody,
    PodiumWinnerInfo,
    Tournament,
    WeightLease,
    WeightLeaseFile,
    WinnerInfo,
)
from nepher_core.custody import (
    CustodyError,
    declared_matches_manifest,
    lease_message,
    safe_relative_path,
    seal_message,
    weights_fingerprint,
)
from validator.evaluation import agent_evaluator as evaluator_module
from validator.evaluation.agent_evaluator import (
    AgentEvaluator,
    EvaluationDeferred,
    EvaluationError,
)
from validator.evaluation.orchestrator import EvaluationOrchestrator
from validator.evaluation.weight_fetch import (
    WeightFetchError,
    WeightMismatchError,
    download_weights,
)
from validator.reward.distribution import custody_payout_blockers

HOTKEY = "5Fhot"
# Same vectors as nepher-cli/tests/test_custody.py and docs/huggingface-weight-custody.md.
VECTOR_WEIGHTS = {
    "model-00001.safetensors": "a" * 64,
    "model-00002.safetensors": "b" * 64,
    "config.json": "c" * 64,
}
VECTOR_FINGERPRINT = "d1997740cfac05556674280cf56da868615abc23da3b301e0f806bef6c30af97"
VECTOR_MESSAGE = (
    f"nepher-seal-v1:{HOTKEY}:tid-1:{VECTOR_FINGERPRINT}:alice/go-weights:{'f' * 40}:1700000000"
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# -- definitions --------------------------------------------------------------


def test_fingerprint_and_seal_vectors():
    assert weights_fingerprint(VECTOR_WEIGHTS, {"c" * 64}) == VECTOR_FINGERPRINT
    renamed = {"x.bin": "b" * 64, "y.bin": "a" * 64, "z.bin": "c" * 64}
    assert weights_fingerprint(renamed, {"C" * 64}) == VECTOR_FINGERPRINT
    message = seal_message(
        HOTKEY, "tid-1", VECTOR_FINGERPRINT, "alice/go-weights", "f" * 40, 1700000000
    )
    assert message == VECTOR_MESSAGE


BASE_ONLY_FINGERPRINT = "9c7995808ddf861b631f6839a193dd893f2a32f816f3e6b1eec8804822c2d30d"


def test_unmodified_base_checkpoint_is_fingerprinted_in_full():
    base = set(VECTOR_WEIGHTS.values())
    assert weights_fingerprint(VECTOR_WEIGHTS, base) == BASE_ONLY_FINGERPRINT
    renamed = {"x.bin": "c" * 64, "y.bin": "a" * 64, "z.bin": "b" * 64}
    assert weights_fingerprint(renamed, {d.upper() for d in base}) == BASE_ONLY_FINGERPRINT
    fine_tune = {**VECTOR_WEIGHTS, "adapter.bin": "d" * 64}
    assert weights_fingerprint(fine_tune, base) == weights_fingerprint({"adapter.bin": "d" * 64})
    with pytest.raises(CustodyError):
        weights_fingerprint({})


def test_same_weights_under_new_names_share_a_fingerprint():
    # Renaming files or splitting them differently cannot dodge duplicate detection.
    first = weights_fingerprint({"a.bin": "1" * 64, "b.bin": "2" * 64})
    second = weights_fingerprint({"renamed/x.bin": "2" * 64, "y.bin": "1" * 64})
    assert first == second
    assert first != weights_fingerprint({"a.bin": "1" * 64, "b.bin": "3" * 64})


def test_lease_message_format():
    assert lease_message("5Val", "agent-1", 1700000000) == "weight-lease:5Val:agent-1:1700000000"


@pytest.mark.parametrize("bad", ["", "/abs", "../up", "a/../../up", "a\\b", "C:/x", "a\x00b"])
def test_safe_relative_path_rejects_escapes(bad):
    with pytest.raises(CustodyError):
        safe_relative_path(bad)


def test_declared_matches_manifest_reports_every_difference():
    assert declared_matches_manifest({"a": "1" * 64}, {"a": "1" * 64}) == []
    errors = declared_matches_manifest(
        {"a": "1" * 64, "extra": "2" * 64, "d": "4" * 64},
        {"a": "1" * 64, "missing": "3" * 64, "d": "5" * 64},
    )
    text = "\n".join(errors)
    assert "not sealed: extra" in text
    assert "missing from agent.yaml: missing" in text
    assert "differs from the sealed hash for d" in text


# -- models and client --------------------------------------------------------


def test_models_default_to_inline_and_accept_custody_fields():
    assert Tournament(id="t", status="active").submission_mode == "inline"
    tournament = Tournament(
        id="t",
        status="active",
        format="vla",
        submission_mode="huggingface",
        base_weight_sha256=["a" * 64],
    )
    assert tournament.base_weight_sha256 == ["a" * 64]
    agent = Agent(
        agent_id="a", custody_status="sealed", hf_revision="f" * 40, weights_fingerprint="x"
    )
    assert agent.custody_status == "sealed"
    assert Agent(id="a").custody_status is None
    info = WinnerInfo(
        winner_approved=True, winners=[{"place": 1, "hotkey": "h", "custody_status": "archived"}]
    )
    assert info.winners[0].custody_status == "archived"


async def test_weight_lease_request_is_signed_and_parsed(monkeypatch):
    api = TournamentAPI(api_key="k", base_url="https://api.example", wallet=object())
    monkeypatch.setattr(api, "_sign_eval_info", lambda info: ("pk", "sig:" + info))
    seen = {}

    async def fake_request(method, path, **kwargs):
        seen.update(method=method, path=path, json=kwargs["json"])
        payload = {
            "agent_id": "a1",
            "revision": "f" * 40,
            "files": [{"path": "w.bin", "sha256": "1" * 64, "size": 3, "url": "https://cdn/w"}],
        }
        return SimpleNamespace(json=lambda: payload)

    monkeypatch.setattr(api, "_request", fake_request)
    lease = await api.request_weight_lease("t1", "a1", "5Val")
    assert seen["method"] == "POST" and seen["path"] == "/api/v1/agents/a1/weight-lease"
    body = seen["json"]
    assert body["lease_info"].startswith("weight-lease:5Val:a1:")
    assert body["signature"] == "sig:" + body["lease_info"]
    assert lease.files[0].path == "w.bin"
    assert api.headers["X-Validator-Custody"] == "1"


# -- brain layout -------------------------------------------------------------


def _agent_files(
    root: Path, weights: dict[str, bytes] | None, declare: dict[str, str] | None = None
) -> Path:
    for name in ("brain", "train"):
        (root / name).mkdir(parents=True, exist_ok=True)
    payload = weights or {}
    if payload:
        for rel, data in payload.items():
            target = root / "weights" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    declared = declare if declare is not None else {rel: _sha(d) for rel, d in payload.items()}
    (root / "agent.yaml").write_text(
        yaml.safe_dump({"entry": "p:P", "horizon": 8, "action_dim": 8, "weights": declared}),
        encoding="utf-8",
    )
    return root


def test_external_weights_layout_allows_absent_weights_but_not_bad_ones(tmp_path: Path):
    declared = {"model.bin": _sha(b"w")}
    absent = _agent_files(tmp_path / "absent", None, declared)
    assert not validate_brain_agent_structure(absent)[0]
    assert validate_brain_agent_structure(absent, external_weights=True)[0]

    wrong = _agent_files(tmp_path / "wrong", {"model.bin": b"other"}, declared)
    ok, errors = validate_brain_agent_structure(wrong, external_weights=True)
    assert not ok and any("sha256 mismatch" in e for e in errors)

    extra = _agent_files(tmp_path / "extra", {"model.bin": b"w", "junk.bin": b"j"}, declared)
    ok, errors = validate_brain_agent_structure(extra, external_weights=True)
    assert not ok and any("no hash in agent.yaml: junk.bin" in e for e in errors)


def test_layout_hashes_weights_by_streaming(tmp_path: Path, monkeypatch):
    agent = _agent_files(tmp_path / "agent", {"model.bin": b"w" * 4096})

    def refuse(self):
        raise AssertionError("weights must be hashed in chunks, not read into memory")

    monkeypatch.setattr(Path, "read_bytes", refuse)
    assert validate_brain_agent_structure(agent)[0]


# -- download -----------------------------------------------------------------


def _lease(files: dict[str, bytes], base: str = "https://cdn.example/") -> WeightLease:
    return WeightLease(
        agent_id="a1",
        revision="f" * 40,
        files=[
            WeightLeaseFile(path=rel, sha256=_sha(data), size=len(data), url=base + rel)
            for rel, data in files.items()
        ],
    )


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _serve(files: dict[str, bytes], seen: list[httpx.Request] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        data = files[request.url.path.lstrip("/")]
        header = request.headers.get("range")
        if header:
            start = int(header.removeprefix("bytes=").rstrip("-"))
            return httpx.Response(206, content=data[start:])
        return httpx.Response(200, content=data)

    return handler


async def test_download_writes_verified_files_without_credentials(tmp_path: Path):
    files = {"model-1.safetensors": b"a" * 5000, "nested/config.json": b"{}"}
    seen: list[httpx.Request] = []
    async with _client(_serve(files, seen)) as client:
        await download_weights(_lease(files), tmp_path / "weights", client=client)
    for rel, data in files.items():
        assert (tmp_path / "weights" / rel).read_bytes() == data
    assert not list((tmp_path / "weights").rglob("*.part"))
    assert seen and all("authorization" not in r.headers for r in seen)


async def test_download_rejects_wrong_bytes_and_leaves_nothing(tmp_path: Path):
    lease = _lease({"w.bin": b"good bytes"})
    async with _client(_serve({"w.bin": b"evil bytes"})) as client:
        with pytest.raises(WeightMismatchError):
            await download_weights(lease, tmp_path / "weights", client=client)
    assert not list((tmp_path / "weights").rglob("w.bin*"))


async def test_download_rejects_oversized_stream(tmp_path: Path):
    lease = _lease({"w.bin": b"12345"})
    async with _client(_serve({"w.bin": b"12345" + b"x" * 100})) as client:
        with pytest.raises(WeightMismatchError):
            await download_weights(lease, tmp_path / "weights", client=client)
    assert not list((tmp_path / "weights").rglob("w.bin*"))


async def test_download_resumes_a_partial_file(tmp_path: Path):
    data = b"0123456789" * 100
    weights = tmp_path / "weights"
    weights.mkdir()
    (weights / "w.bin.part").write_bytes(data[:300])
    seen: list[httpx.Request] = []
    async with _client(_serve({"w.bin": data}, seen)) as client:
        await download_weights(_lease({"w.bin": data}), weights, client=client)
    assert seen[0].headers["range"] == "bytes=300-"
    assert (weights / "w.bin").read_bytes() == data


async def test_download_restarts_when_server_ignores_range(tmp_path: Path):
    data = b"abcdefghij" * 50
    weights = tmp_path / "weights"
    weights.mkdir()
    (weights / "w.bin.part").write_bytes(data[:100])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=data)

    async with _client(handler) as client:
        await download_weights(_lease({"w.bin": data}), weights, client=client)
    assert (weights / "w.bin").read_bytes() == data


async def test_download_retries_a_truncated_stream_with_range(tmp_path: Path):
    data = b"z" * 1000
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers.get("range"))
        if len(calls) == 1:
            return httpx.Response(200, content=data[:400])
        start = int(request.headers["range"].removeprefix("bytes=").rstrip("-"))
        return httpx.Response(206, content=data[start:])

    async with _client(handler) as client:
        await download_weights(_lease({"w.bin": data}), tmp_path / "weights", client=client)
    assert calls == [None, "bytes=400-"]
    assert (tmp_path / "weights" / "w.bin").read_bytes() == data


async def test_download_refreshes_an_expired_url(tmp_path: Path):
    data = b"payload"
    stale = _lease({"w.bin": data}, base="https://stale.example/")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "stale.example":
            return httpx.Response(403)
        return httpx.Response(200, content=data)

    refresh = AsyncMock(return_value=_lease({"w.bin": data}, base="https://fresh.example/"))
    async with _client(handler) as client:
        await download_weights(stale, tmp_path / "weights", refresh_lease=refresh, client=client)
    assert refresh.await_count == 1
    assert (tmp_path / "weights" / "w.bin").read_bytes() == data


async def test_download_fails_when_expired_without_refresh_or_manifest_changes(tmp_path: Path):
    data = b"payload"

    def expired(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    async with _client(expired) as client:
        with pytest.raises(WeightFetchError):
            await download_weights(_lease({"w.bin": data}), tmp_path / "a", client=client)
        changed = AsyncMock(return_value=_lease({"w.bin": b"different"}))
        with pytest.raises(WeightMismatchError):
            await download_weights(
                _lease({"w.bin": data}), tmp_path / "b", refresh_lease=changed, client=client
            )


@pytest.mark.parametrize(
    "lease",
    [
        _lease({"../escape.bin": b"x"}),
        _lease({"/abs.bin": b"x"}),
        _lease({"w.bin": b"x"}, base="http://insecure.example/"),
    ],
)
async def test_download_rejects_unsafe_leases(tmp_path: Path, lease):
    async with _client(lambda request: httpx.Response(200, content=b"x")) as client:
        with pytest.raises(WeightFetchError):
            await download_weights(lease, tmp_path / "weights", client=client)
    assert not (tmp_path / "escape.bin").exists()


async def test_download_refuses_redirects(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example/w.bin"})

    async with _client(handler) as client:
        with pytest.raises(WeightFetchError):
            await download_weights(_lease({"w.bin": b"x"}), tmp_path / "weights", client=client)


# -- evaluator gate -----------------------------------------------------------

WEIGHTS = {"model.bin": b"sealed weights"}
FINGERPRINT = weights_fingerprint({rel: _sha(d) for rel, d in WEIGHTS.items()})


class FakeAPI:
    def __init__(self, lease: WeightLease | None = None, statuses: list[str] | None = None):
        self.lease = lease or _lease(WEIGHTS)
        self.statuses = statuses or ["sealed"]
        self.custody_reads = 0
        self.lease_requests = 0
        self.released = 0
        self.submitted: list[dict] = []

    async def get_agent_custody(self, agent_id):
        status = self.statuses[min(self.custody_reads, len(self.statuses) - 1)]
        self.custody_reads += 1
        return AgentCustody(
            agent_id=agent_id, custody_status=status, weights_fingerprint=FINGERPRINT
        )

    async def request_weight_lease(self, **kwargs):
        self.lease_requests += 1
        return self.lease

    async def release_weight_lease(self, **kwargs):
        self.released += 1

    async def set_evaluation_in_progress(self, **kwargs):
        pass

    async def clear_evaluation_in_progress(self, **kwargs):
        pass

    async def get_whitelist_domains(self):
        return []

    async def submit_evaluation(self, **kwargs):
        self.submitted.append(kwargs)


def _make_evaluator(
    tmp_path: Path,
    api: FakeAPI,
    *,
    runtime: str = "brain",
    max_gb: float = 1.0,
    zip_weights: dict[str, bytes] | None = None,
    monkeypatch=None,
):
    evaluator = AgentEvaluator.__new__(AgentEvaluator)
    tournament_dir = tmp_path / "tn"
    evaluator.config = SimpleNamespace(
        paths=SimpleNamespace(tournament_workspace=lambda tid: tournament_dir),
        retry=SimpleNamespace(evaluation_timeout_seconds=60),
    )
    evaluator.api = api
    evaluator.validator_hotkey = "5Val"
    evaluator.workspace = tmp_path
    evaluator._deferred_until = {}
    evaluator.sandbox = SimpleNamespace(
        verify_docker=AsyncMock(),
        ensure_brain_image=AsyncMock(),
        run_brain_evaluation=AsyncMock(return_value={"score": 0.9, "metadata": {}, "summary": ""}),
    )
    evaluator._set_tournament_context("tid-1")
    task = {
        "runtime": runtime,
        "task_module": "m",
        "brain": {"brain_image": "img", "task_package": "pkg", "max_submission_gb": max_gb},
    }
    (tournament_dir / "task_config.yaml").write_text(yaml.safe_dump(task), encoding="utf-8")

    async def prepare(agent):
        _agent_files(
            evaluator.registry_path, zip_weights, {rel: _sha(d) for rel, d in WEIGHTS.items()}
        )

    async def fake_download(lease, destination, **kwargs):
        for rel, data in WEIGHTS.items():
            (destination / rel).parent.mkdir(parents=True, exist_ok=True)
            (destination / rel).write_bytes(data)

    evaluator._prepare_agent = prepare
    evaluator._build_eval_config = lambda: tournament_dir / "eval_config.yaml"
    evaluator._submit_results = AsyncMock()
    monkeypatch.setattr("validator.evaluation.weight_fetch.download_weights", fake_download)
    return evaluator


def _hf_agent(**overrides) -> Agent:
    payload = {"id": "a1", "custody_status": "sealed", "weights_fingerprint": FINGERPRINT}
    payload.update(overrides)
    return Agent(**payload)


async def _run(evaluator, agent=None, mode="huggingface"):
    await evaluator.evaluate("tid-1", agent or _hf_agent(), submission_mode=mode)


async def test_sealed_agent_downloads_weights_evaluates_and_submits(tmp_path, monkeypatch):
    api = FakeAPI()
    evaluator = _make_evaluator(tmp_path, api, monkeypatch=monkeypatch)
    await _run(evaluator)
    evaluator.sandbox.run_brain_evaluation.assert_awaited_once()
    evaluator._submit_results.assert_awaited_once()
    assert api.released == 1
    assert api.custody_reads == 2  # before the download and again before submitting


async def test_result_is_dropped_when_custody_is_lost_during_evaluation(tmp_path, monkeypatch):
    api = FakeAPI(statuses=["sealed", "disqualified"])
    evaluator = _make_evaluator(tmp_path, api, monkeypatch=monkeypatch)
    with pytest.raises(EvaluationDeferred):
        await _run(evaluator)
    evaluator.sandbox.run_brain_evaluation.assert_awaited_once()
    evaluator._submit_results.assert_not_awaited()


async def test_unsealed_agent_never_reaches_download_and_cools_down(tmp_path, monkeypatch):
    api = FakeAPI()
    evaluator = _make_evaluator(tmp_path, api, monkeypatch=monkeypatch)
    with pytest.raises(EvaluationDeferred):
        await _run(evaluator, _hf_agent(custody_status="disqualified"))
    assert api.lease_requests == 0
    # A sealed agent that failed a custody check is not retried straight away.
    api.statuses = ["disqualified"]
    with pytest.raises(EvaluationDeferred):
        await _run(evaluator)
    reads = api.custody_reads
    with pytest.raises(EvaluationDeferred):
        await _run(evaluator)
    assert api.custody_reads == reads


async def test_manifest_that_differs_from_agent_yaml_is_reported_before_download(
    tmp_path, monkeypatch
):
    other = _lease({"model.bin": b"someone else's weights"})
    api = FakeAPI(lease=other)
    evaluator = _make_evaluator(tmp_path, api, monkeypatch=monkeypatch)
    with pytest.raises(EvaluationDeferred) as excinfo:
        await _run(evaluator)
    assert excinfo.value.report_type == "weights_mismatch"
    evaluator.sandbox.run_brain_evaluation.assert_not_awaited()
    assert api.released == 1


async def test_download_mismatch_is_reported_not_scored(tmp_path, monkeypatch):
    api = FakeAPI()
    evaluator = _make_evaluator(tmp_path, api, monkeypatch=monkeypatch)

    async def tampered(lease, destination, **kwargs):
        raise WeightMismatchError("model.bin does not match the sealed sha256")

    monkeypatch.setattr("validator.evaluation.weight_fetch.download_weights", tampered)
    with pytest.raises(EvaluationDeferred) as excinfo:
        await _run(evaluator)
    assert excinfo.value.report_type == "weights_mismatch"
    evaluator._submit_results.assert_not_awaited()


async def test_lease_refusal_defers_without_scoring(tmp_path, monkeypatch):
    api = FakeAPI()
    evaluator = _make_evaluator(tmp_path, api, monkeypatch=monkeypatch)

    async def refused(**kwargs):
        raise APIError("not_sealed", status_code=409)

    api.request_weight_lease = refused
    with pytest.raises(EvaluationDeferred):
        await _run(evaluator)
    evaluator._submit_results.assert_not_awaited()


async def test_archive_with_weights_is_rejected(tmp_path, monkeypatch):
    evaluator = _make_evaluator(
        tmp_path, FakeAPI(), zip_weights={"model.bin": b"x"}, monkeypatch=monkeypatch
    )
    with pytest.raises(EvaluationError, match="must not contain weights"):
        await _run(evaluator)


async def test_sealed_weights_larger_than_the_limit_are_rejected(tmp_path, monkeypatch):
    evaluator = _make_evaluator(tmp_path, FakeAPI(), max_gb=1e-9, monkeypatch=monkeypatch)
    with pytest.raises(EvaluationError, match="limit"):
        await _run(evaluator)


async def test_mode_and_runtime_mismatches_are_errors(tmp_path, monkeypatch):
    evaluator = _make_evaluator(tmp_path, FakeAPI(), monkeypatch=monkeypatch)
    with pytest.raises(EvaluationError, match="unknown submission_mode"):
        await _run(evaluator, mode="ipfs")
    with pytest.raises(EvaluationError, match="custody status"):
        await _run(evaluator, mode="inline")
    plain = _make_evaluator(
        tmp_path / "plain", FakeAPI(), runtime="in_process", monkeypatch=monkeypatch
    )
    with pytest.raises(EvaluationError, match="runtime: brain"):
        await _run(plain)


async def test_inline_brain_evaluation_does_not_touch_custody(tmp_path, monkeypatch):
    api = FakeAPI()
    evaluator = _make_evaluator(tmp_path, api, monkeypatch=monkeypatch)

    async def prepare(agent):
        _agent_files(evaluator.registry_path, WEIGHTS)

    evaluator._prepare_agent = prepare
    await _run(evaluator, Agent(id="a1"), mode="inline")
    evaluator._submit_results.assert_awaited_once()
    assert api.custody_reads == api.lease_requests == api.released == 0


def test_evaluator_module_does_not_pass_credentials_to_the_sandbox():
    source = Path(evaluator_module.__file__).read_text(encoding="utf-8")
    assert "hf_read_token" not in source and "HF_TOKEN" not in source


# -- orchestrator -------------------------------------------------------------


def _orchestrator(evaluate) -> EvaluationOrchestrator:
    orchestrator = EvaluationOrchestrator.__new__(EvaluationOrchestrator)
    orchestrator.evaluator = SimpleNamespace(evaluate=evaluate)
    orchestrator.api = SimpleNamespace(
        submit_failed_evaluation=AsyncMock(), report_agent=AsyncMock()
    )
    orchestrator.validator_hotkey = "5Val"
    orchestrator._evaluated_count = 0
    orchestrator._failed_count = 0
    return orchestrator


async def test_orchestrator_passes_tournament_mode_and_defers_without_a_failed_score():
    calls = []

    async def evaluate(**kwargs):
        calls.append(kwargs)
        raise EvaluationDeferred("weights changed", "weights_mismatch")

    orchestrator = _orchestrator(evaluate)
    tournament = Tournament(
        id="t", status="active", submission_mode="huggingface", base_weight_sha256=["a" * 64]
    )
    await orchestrator._evaluate_one("t", Agent(id="a1"), tournament)
    assert calls[0]["submission_mode"] == "huggingface"
    assert calls[0]["base_weight_sha256"] == ["a" * 64]
    orchestrator.api.submit_failed_evaluation.assert_not_awaited()
    assert orchestrator.api.report_agent.await_args.kwargs["error_type"] == "weights_mismatch"
    assert orchestrator._failed_count == 0


async def test_orchestrator_keeps_the_original_call_without_a_tournament():
    calls = []

    async def evaluate(**kwargs):
        calls.append(kwargs)

    await _orchestrator(evaluate)._evaluate_one("t", Agent(id="a1"))
    assert set(calls[0]) == {"tournament_id", "agent"}


# -- reward gate --------------------------------------------------------------


def _place(
    place: int, status: str | None, share: float = 1.0, hotkey: str | None = "h"
) -> PodiumWinnerInfo:
    return PodiumWinnerInfo(place=place, hotkey=hotkey, reward_share=share, custody_status=status)


def test_inline_podium_is_never_gated():
    assert custody_payout_blockers([_place(1, None)], "inline") == []


def test_huggingface_podium_needs_every_paid_place_archived():
    assert (
        custody_payout_blockers(
            [_place(1, "archived", 0.6), _place(2, "archived", 0.4)], "huggingface"
        )
        == []
    )
    blocked = custody_payout_blockers(
        [_place(1, "archived", 0.6), _place(2, "sealed", 0.4)], "huggingface"
    )
    assert [p.place for p in blocked] == [2]
    assert custody_payout_blockers([_place(1, None)], "huggingface")[0].place == 1


def test_podium_with_a_custody_status_is_gated_even_in_inline_mode():
    assert custody_payout_blockers([_place(1, "disqualified")], "inline")
    # Unpaid places and places without a hotkey cannot block.
    assert (
        custody_payout_blockers([_place(1, "archived"), _place(2, "sealed", 0.0)], "inline") == []
    )
    assert (
        custody_payout_blockers([_place(1, "archived"), _place(2, "sealed", hotkey=None)], "inline")
        == []
    )
