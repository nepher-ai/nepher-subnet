#!/usr/bin/env python3
"""
Test script for the two-phase evaluation flow.

Supports all validator-facing endpoints: in-progress signaling, evaluation
submission (verify → submit), fetching unevaluated agents, downloading agents,
fetching the active tournament & eval config, leaderboard, and winner hotkey.

The backend derives the evaluation *phase* ("public" or "private") from the
current timestamp.  During the quiet zone (between public eval cutoff and
private eval start) all submission endpoints return 409 Conflict.

Use --phase public|private with --fetch-unevaluated and --fetch-leaderboard
to scope results to a single phase.  Without it, --fetch-unevaluated returns
agents not yet evaluated in *any* phase, and --fetch-leaderboard auto-detects
the best phase for display.

Usage:
  # ── Evaluation flow ──────────────────────────────────────────

  # In-progress only
  python scripts/test_evaluation_submit.py \\
    -t <UUID> -a <UUID> --in-progress \\
    --api-key <KEY> --wallet-name X --wallet-hotkey Y

  # Submit result only
  python scripts/test_evaluation_submit.py \\
    -t <UUID> -a <UUID> --submit --score 0.85 \\
    --api-key <KEY> --wallet-name X --wallet-hotkey Y

  # Full flow: in-progress → submit
  python scripts/test_evaluation_submit.py \\
    -t <UUID> -a <UUID> --in-progress --submit --score 0.9 \\
    --api-key <KEY> --wallet-name X --wallet-hotkey Y

  # Clear in-progress (omit --agent-id)
  python scripts/test_evaluation_submit.py \\
    -t <UUID> --in-progress --api-key <KEY> --wallet-name X --wallet-hotkey Y

  # ── Queries (read-only) ─────────────────────────────────────

  # Fetch unevaluated agents (all phases)
  python scripts/test_evaluation_submit.py \\
    -t <UUID> --fetch-unevaluated \\
    --api-key <KEY> --validator-hotkey-ss58 <SS58>

  # Fetch unevaluated agents for a specific phase
  python scripts/test_evaluation_submit.py \\
    -t <UUID> --fetch-unevaluated --phase private \\
    --api-key <KEY> --validator-hotkey-ss58 <SS58>

  # Fetch leaderboard (auto-detect or explicit phase)
  python scripts/test_evaluation_submit.py \\
    -t <UUID> --fetch-leaderboard --phase public

  # Fetch winner hotkey (public, no auth needed)
  python scripts/test_evaluation_submit.py \\
    -t <UUID> --fetch-winner

  # Fetch active eval config (phase-appropriate YAML)
  python scripts/test_evaluation_submit.py \\
    -t <UUID> --fetch-active-config

  # ── Tournament & agent ──────────────────────────────────────

  # Fetch active tournament (full response, no --tournament-id needed)
  python scripts/test_evaluation_submit.py --fetch-active-tournament

  # Fetch active tournament (subnet-optimized minimal payload)
  python scripts/test_evaluation_submit.py --fetch-active-tournament --subnet

  # Download agent zip
  python scripts/test_evaluation_submit.py \\
    -t <UUID> -a <UUID> --download-agent \\
    --api-key <KEY> --output-dir ./downloads
"""

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import UUID

try:
    import httpx
except ImportError:
    print("Error: httpx not installed. Run: pip install httpx")
    sys.exit(1)

try:
    from bittensor_wallet import Wallet
except ImportError:
    print("Error: bittensor-wallet not installed. Run: pip install bittensor-wallet")
    sys.exit(1)

DEFAULT_SERVER_URL = "http://localhost:8003"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _validate_uuid(value: str, name: str) -> str:
    try:
        UUID(value)
        return value
    except ValueError:
        raise ValueError(f"Invalid {name}: must be a valid UUID")


def _fmt_ts(ts: Optional[int]) -> str:
    """Format a Unix timestamp for display, or 'N/A'."""
    if ts is None:
        return "N/A"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _raise_api_error(context: str, response: httpx.Response) -> None:
    """Raise a RuntimeError with a human-readable message from an API error response."""
    try:
        detail = response.json().get("detail", response.text)
    except Exception:
        detail = response.text
    status_hint = ""
    if response.status_code == 409:
        status_hint = " [quiet zone — no evaluations accepted]"
    raise RuntimeError(f"{context} ({response.status_code}{status_hint}): {detail}")


def create_eval_info(
    hotkey: str,
    tournament_id: str,
    agent_id: str,
    timestamp: int,
    log_hash: Optional[str] = None,
) -> str:
    """eval_info string for signing: hotkey:tournament_id:agent_id:timestamp[:log_hash]."""
    base = f"{hotkey}:{tournament_id}:{agent_id}:{timestamp}"
    return f"{base}:{log_hash}" if log_hash else base


def sign_eval_info(wallet: Wallet, eval_info: str) -> tuple[str, str]:
    """Sign eval_info with wallet hotkey. Returns (public_key_hex, signature_hex)."""
    public_key = wallet.hotkey.public_key.hex()
    signature = wallet.hotkey.sign(eval_info).hex()
    return public_key, signature


def calculate_file_hash(file_path: Path) -> str:
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


# ---------------------------------------------------------------------------
# API calls
# ---------------------------------------------------------------------------

def post_in_progress(
    server_url: str,
    api_key: str,
    tournament_id: str,
    agent_id: Optional[str],
    validator_hotkey: str,
    public_key: str,
    eval_info: str,
    signature: str,
) -> dict:
    """POST /api/v1/evaluations/in-progress/cli"""
    with httpx.Client(timeout=30) as client:
        response = client.post(
            f"{server_url}/api/v1/evaluations/in-progress/cli",
            headers={"X-API-Key": api_key},
            json={
                "tournament_id": tournament_id,
                "agent_id": agent_id,
                "validator_hotkey": validator_hotkey,
                "public_key": public_key,
                "eval_info": eval_info,
                "signature": signature,
            },
        )
        if response.status_code != 200:
            _raise_api_error("In-progress failed", response)
        return response.json()


def request_upload_token(
    server_url: str,
    api_key: str,
    validator_hotkey: str,
    public_key: str,
    eval_info: str,
    signature: str,
    tournament_id: str,
    agent_id: str,
    log_file_size: Optional[int] = None,
) -> dict:
    """POST /api/v1/evaluations/submit/verify"""
    with httpx.Client(timeout=30) as client:
        response = client.post(
            f"{server_url}/api/v1/evaluations/submit/verify",
            headers={"X-API-Key": api_key},
            json={
                "validator_hotkey": validator_hotkey,
                "public_key": public_key,
                "eval_info": eval_info,
                "signature": signature,
                "tournament_id": tournament_id,
                "agent_id": agent_id,
                "log_file_size": log_file_size,
            },
        )
        if response.status_code != 200:
            _raise_api_error("Verify failed", response)
        return response.json()


def submit_evaluation(
    server_url: str,
    upload_token: str,
    validator_hotkey: str,
    score: Optional[float],
    metadata: Optional[dict],
    summary: Optional[str],
    log_file_path: Optional[Path],
    log_hash: Optional[str],
) -> dict:
    """POST /api/v1/evaluations/submit (multipart with X-Upload-Token)"""
    with httpx.Client(timeout=300) as client:
        data = {"validator_hotkey": validator_hotkey}
        if score is not None:
            data["score"] = str(score)
        if metadata is not None:
            data["metadata"] = json.dumps(metadata)
        if summary is not None:
            data["summary"] = summary
        if log_hash is not None:
            data["log_hash"] = log_hash

        files = None
        if log_file_path and log_file_path.exists():
            files = {"log_file": (log_file_path.name, open(log_file_path, "rb"), "application/zip")}

        try:
            response = client.post(
                f"{server_url}/api/v1/evaluations/submit",
                headers={"X-Upload-Token": upload_token},
                data=data,
                files=files,
            )
        finally:
            if files:
                files["log_file"][1].close()

        if response.status_code not in (200, 201):
            _raise_api_error("Submit failed", response)
        return response.json()


def fetch_unevaluated_agents(
    server_url: str,
    api_key: str,
    tournament_id: str,
    validator_hotkey: str,
    phase: Optional[str] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
) -> dict:
    """GET /api/v1/agents/list/unevaluated"""
    with httpx.Client(timeout=30) as client:
        params: dict = {
            "tournament_id": tournament_id,
            "validator_hotkey": validator_hotkey,
        }
        if phase is not None:
            params["phase"] = phase
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset

        response = client.get(
            f"{server_url}/api/v1/agents/list/unevaluated",
            headers={"X-API-Key": api_key},
            params=params,
        )
        if response.status_code != 200:
            _raise_api_error("Fetch unevaluated agents failed", response)
        return response.json()


def fetch_winner(server_url: str, tournament_id: str) -> dict:
    """GET /api/v1/tournaments/{tournament_id}/winner-hotkey"""
    with httpx.Client(timeout=30) as client:
        response = client.get(f"{server_url}/api/v1/tournaments/{tournament_id}/winner-hotkey")
        if response.status_code != 200:
            _raise_api_error("Fetch winner failed", response)
        return response.json()


def fetch_active_config(server_url: str, tournament_id: str) -> tuple[str, str]:
    """GET /api/v1/tournaments/{tournament_id}/config/active_eval_config

    Returns (phase_from_header, yaml_content).
    """
    with httpx.Client(timeout=30) as client:
        response = client.get(f"{server_url}/api/v1/tournaments/{tournament_id}/config/active_eval_config")
        if response.status_code != 200:
            _raise_api_error("Fetch active config failed", response)
        phase = response.headers.get("x-eval-phase", "unknown")
        return phase, response.text


def fetch_leaderboard(
    server_url: str,
    tournament_id: str,
    phase: Optional[str] = None,
    limit: int = 10,
) -> dict:
    """GET /api/v1/scores/leaderboard/{tournament_id}"""
    with httpx.Client(timeout=30) as client:
        params: dict = {"limit": limit}
        if phase:
            params["phase"] = phase
        response = client.get(
            f"{server_url}/api/v1/scores/leaderboard/{tournament_id}",
            params=params,
        )
        if response.status_code != 200:
            _raise_api_error("Fetch leaderboard failed", response)
        return response.json()


def fetch_active_tournament(server_url: str, subnet: bool = False) -> dict:
    """GET /api/v1/tournaments/active"""
    with httpx.Client(timeout=30) as client:
        params: dict = {}
        if subnet:
            params["subnet"] = "true"
        response = client.get(
            f"{server_url}/api/v1/tournaments/active",
            params=params,
        )
        if response.status_code != 200:
            _raise_api_error("Fetch active tournament failed", response)
        data = response.json()
        if data is None:
            raise RuntimeError("No active tournament found")
        return data


def download_agent(
    server_url: str,
    api_key: str,
    agent_id: str,
    output_dir: Path,
) -> Path:
    """GET /api/v1/agents/download/{agent_id} → save to output_dir."""
    output_dir.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=120, follow_redirects=True) as client:
        response = client.get(
            f"{server_url}/api/v1/agents/download/{agent_id}",
            headers={"X-API-Key": api_key},
        )
        if response.status_code != 200:
            _raise_api_error("Download agent failed", response)

        cd = response.headers.get("content-disposition", "")
        if "filename=" in cd:
            filename = cd.split("filename=")[-1].strip('" ')
        else:
            filename = f"agent_{agent_id}.zip"

        dest = output_dir / filename
        dest.write_bytes(response.content)
        return dest


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Test the two-phase evaluation flow (in-progress, submit, fetch)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--tournament-id", "-t", help="Tournament ID (UUID). Not required for --fetch-active-tournament.")
    parser.add_argument(
        "--agent-id", "-a",
        help="Agent ID (UUID). Required for --submit; optional for --in-progress (omit to clear).",
    )

    # Actions
    actions = parser.add_argument_group("actions (at least one required)")
    actions.add_argument("--in-progress", action="store_true", help="Send in-progress signal")
    actions.add_argument("--submit", action="store_true", help="Submit evaluation result (verify → submit)")
    actions.add_argument("--fetch-unevaluated", action="store_true", help="Fetch unevaluated agents for this validator")
    actions.add_argument("--fetch-winner", action="store_true", help="Fetch winner hotkey (public, no auth needed)")
    actions.add_argument("--fetch-active-config", action="store_true", help="Fetch the phase-appropriate eval config")
    actions.add_argument("--fetch-leaderboard", action="store_true", help="Fetch leaderboard (supports --phase)")
    actions.add_argument("--fetch-active-tournament", action="store_true", help="Fetch active tournament details")
    actions.add_argument("--download-agent", action="store_true", help="Download agent zip (requires --agent-id, --api-key)")

    # Submission options
    sub_opts = parser.add_argument_group("submission options")
    sub_opts.add_argument("--score", "-s", type=float, default=0.0, help="Score for submit (default: 0.0)")
    sub_opts.add_argument("--summary", help="Optional summary text for submit")
    sub_opts.add_argument("--metadata", help="Optional JSON object for submit metadata")
    sub_opts.add_argument("--log-file", "-l", help="Optional log file (zip) for submit")

    # Connection / auth
    conn = parser.add_argument_group("connection and authentication")
    conn.add_argument("--api-key", help="Validator API key")
    conn.add_argument("--server-url", default=DEFAULT_SERVER_URL, help=f"Backend URL (default: {DEFAULT_SERVER_URL})")
    conn.add_argument("--wallet-name", help="Bittensor wallet name")
    conn.add_argument("--wallet-hotkey", help="Bittensor hotkey name")
    conn.add_argument("--validator-hotkey-ss58", help="Validator SS58 address (alternative to wallet)")

    # Extra options
    extra = parser.add_argument_group("extra options")
    extra.add_argument(
        "--phase",
        choices=["public", "private"],
        help="Phase filter for --fetch-leaderboard and --fetch-unevaluated",
    )
    extra.add_argument(
        "--subnet",
        action="store_true",
        help="Use subnet-optimized response for --fetch-active-tournament",
    )
    extra.add_argument(
        "--output-dir", "-o",
        default="./downloads",
        help="Directory to save downloaded agent zip (default: ./downloads)",
    )

    # Ignore empty/whitespace args (e.g. from line continuation or copy-paste)
    argv = [a for a in sys.argv[1:] if a and not a.isspace()]
    args = parser.parse_args(argv)

    has_action = (
        args.in_progress or args.submit or args.fetch_unevaluated
        or args.fetch_winner or args.fetch_active_config or args.fetch_leaderboard
        or args.fetch_active_tournament or args.download_agent
    )
    if not has_action:
        parser.error(
            "Specify at least one action: --in-progress, --submit, --fetch-unevaluated, "
            "--fetch-winner, --fetch-active-config, --fetch-leaderboard, "
            "--fetch-active-tournament, or --download-agent"
        )
    if args.submit and not args.agent_id:
        parser.error("--agent-id is required for --submit")
    if args.download_agent and not args.agent_id:
        parser.error("--agent-id is required for --download-agent")
    if args.download_agent and not args.api_key:
        parser.error("--api-key is required for --download-agent")

    needs_tournament_id = (
        args.in_progress or args.submit or args.fetch_unevaluated
        or args.fetch_winner or args.fetch_active_config or args.fetch_leaderboard
        or args.download_agent
    )
    if needs_tournament_id and not args.tournament_id:
        parser.error("--tournament-id is required for this action")

    needs_wallet = args.in_progress or args.submit
    if needs_wallet and (not args.wallet_name or not args.wallet_hotkey):
        parser.error("--wallet-name and --wallet-hotkey are required for --in-progress / --submit")

    needs_hotkey = args.fetch_unevaluated
    if needs_hotkey and not needs_wallet and not args.validator_hotkey_ss58 and not (args.wallet_name and args.wallet_hotkey):
        parser.error("Provide --validator-hotkey-ss58 or --wallet-name/--wallet-hotkey for --fetch-unevaluated")

    needs_api_key = args.in_progress or args.submit or args.fetch_unevaluated
    if needs_api_key and not args.api_key:
        parser.error("--api-key is required for --in-progress, --submit, and --fetch-unevaluated")

    # UUID validation
    if args.in_progress and not args.agent_id and not args.submit:
        pass  # clearing in-progress — agent_id can be None
    elif args.agent_id:
        try:
            _validate_uuid(args.agent_id, "agent_id")
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

    tournament_id = _validate_uuid(args.tournament_id, "tournament_id") if args.tournament_id else None
    agent_id = args.agent_id

    metadata = None
    if args.metadata:
        try:
            metadata = json.loads(args.metadata)
            if not isinstance(metadata, dict):
                print("Error: --metadata must be a JSON object", file=sys.stderr)
                return 1
        except json.JSONDecodeError as e:
            print(f"Error: Invalid JSON in --metadata: {e}", file=sys.stderr)
            return 1

    log_file_path = Path(args.log_file) if args.log_file else None
    log_hash = None
    log_file_size = None
    if log_file_path and log_file_path.exists():
        log_hash = calculate_file_hash(log_file_path)
        log_file_size = log_file_path.stat().st_size

    print(f"Tournament: {tournament_id or '(auto — active)'}")
    print(f"Agent:      {agent_id or '(N/A)'}")
    print(f"Server:     {args.server_url}")
    print()

    # Resolve hotkey
    wallet = None
    hotkey_ss58 = None

    if args.wallet_name and args.wallet_hotkey:
        try:
            wallet = Wallet(name=args.wallet_name, hotkey=args.wallet_hotkey)
            hotkey_ss58 = wallet.hotkey.ss58_address
        except Exception as e:
            print(f"Error loading wallet: {e}", file=sys.stderr)
            return 1
    elif args.validator_hotkey_ss58:
        hotkey_ss58 = args.validator_hotkey_ss58

    if hotkey_ss58:
        print(f"Validator:  {hotkey_ss58}")
        print()

    # Signing data (only when wallet is available)
    timestamp = None
    public_key = None
    signature_in_progress = None
    eval_info_in_progress = None

    if wallet:
        timestamp = int(time.time())
        eval_info_in_progress = create_eval_info(hotkey_ss58, tournament_id, agent_id or "", timestamp)
        public_key, signature_in_progress = sign_eval_info(wallet, eval_info_in_progress)

    # ── In-progress ──────────────────────────────────────────────
    if args.in_progress:
        print("=" * 50)
        print("IN-PROGRESS")
        print("=" * 50)
        try:
            result = post_in_progress(
                args.server_url,
                args.api_key,
                tournament_id,
                None if not agent_id else agent_id,
                hotkey_ss58,
                public_key,
                eval_info_in_progress,
                signature_in_progress,
            )
            print(f"  Result:  {result.get('message', result)}")
            if result.get("phase"):
                print(f"  Phase:   {result['phase']}")
        except Exception as e:
            print(f"  FAILED: {e}", file=sys.stderr)
            return 1
        print()

    # ── Submit (verify → submit) ─────────────────────────────────
    if args.submit:
        print("=" * 50)
        print("SUBMIT EVALUATION")
        print("=" * 50)

        eval_info_submit = create_eval_info(hotkey_ss58, tournament_id, agent_id, timestamp, log_hash)
        public_key2, signature_submit = sign_eval_info(wallet, eval_info_submit)

        print("Step 1: Requesting upload token (verify)...")
        try:
            token_resp = request_upload_token(
                args.server_url,
                args.api_key,
                hotkey_ss58,
                public_key2,
                eval_info_submit,
                signature_submit,
                tournament_id,
                agent_id,
                log_file_size,
            )
        except Exception as e:
            print(f"  FAILED: {e}", file=sys.stderr)
            return 1

        upload_token = token_resp["upload_token"]
        locked_phase = token_resp.get("phase", "unknown")
        pe_end = token_resp.get("public_eval_end_time")

        print(f"  Token:             {upload_token[:24]}...")
        print(f"  Phase (locked):    {locked_phase}")
        if pe_end:
            print(f"  Public eval ends:  {_fmt_ts(pe_end)}")
        if token_resp.get("existing_status"):
            print(f"  Existing status:   {token_resp['existing_status']}")
        print()

        print("Step 2: Submitting evaluation...")
        try:
            result = submit_evaluation(
                args.server_url,
                upload_token,
                hotkey_ss58,
                args.score,
                metadata,
                args.summary,
                log_file_path,
                log_hash,
            )
            print(f"  Evaluation ID:  {result['evaluation_id']}")
            print(f"  Phase:          {result.get('phase', 'N/A')}")
            print(f"  Status:         {result['status']}")
            print(f"  Score:          {result.get('score')}")
            if result.get("evaluated_at"):
                print(f"  Evaluated at:   {result['evaluated_at']}")
        except Exception as e:
            print(f"  FAILED: {e}", file=sys.stderr)
            return 1
        print()

    # ── Fetch unevaluated agents ─────────────────────────────────
    if args.fetch_unevaluated:
        phase_label = f" (phase={args.phase})" if args.phase else ""
        print("=" * 50)
        print(f"UNEVALUATED AGENTS{phase_label}")
        print("=" * 50)
        try:
            result = fetch_unevaluated_agents(
                args.server_url,
                args.api_key,
                tournament_id,
                hotkey_ss58,
                phase=args.phase,
            )
            agents = result.get("agents", [])
            total = result.get("total", 0)
            print(f"  Total: {total}")
            if agents:
                print()
                for i, agent in enumerate(agents, 1):
                    print(f"  [{i}] Agent ID:      {agent['id']}")
                    print(f"      Miner Hotkey:  {agent.get('miner_hotkey', 'N/A')}")
                    print(f"      Task:          {agent.get('task_name', 'N/A')}")
                    print(f"      Status:        {agent.get('status', 'N/A')}")
                    print(f"      Uploaded At:   {agent.get('uploaded_at', 'N/A')}")
                    print()
            else:
                print("  All agents have been evaluated by this validator.")
        except Exception as e:
            print(f"  FAILED: {e}", file=sys.stderr)
            return 1
        print()

    # ── Fetch winner ─────────────────────────────────────────────
    if args.fetch_winner:
        print("=" * 50)
        print("WINNER")
        print("=" * 50)
        try:
            result = fetch_winner(args.server_url, tournament_id)
            print(f"  Winner Approved:  {result.get('winner_approved')}")
            print(f"  Winner Hotkey:    {result.get('winner_hotkey') or 'N/A'}")
            print(f"  Winner Agent ID:  {result.get('winner_agent_id') or 'N/A'}")
            print(f"  Winner Score:     {result.get('winner_score') if result.get('winner_score') is not None else 'N/A'}")
        except Exception as e:
            print(f"  FAILED: {e}", file=sys.stderr)
            return 1
        print()

    # ── Fetch active eval config ─────────────────────────────────
    if args.fetch_active_config:
        print("=" * 50)
        print("ACTIVE EVAL CONFIG")
        print("=" * 50)
        try:
            phase, yaml_content = fetch_active_config(args.server_url, tournament_id)
            print(f"  Phase:    {phase}")
            print(f"  Length:   {len(yaml_content)} bytes")
            print()
            # Show a preview (first 20 lines)
            lines = yaml_content.splitlines()
            preview = lines[:20]
            for line in preview:
                print(f"  | {line}")
            if len(lines) > 20:
                print(f"  | ... ({len(lines) - 20} more lines)")
        except Exception as e:
            print(f"  FAILED: {e}", file=sys.stderr)
            return 1
        print()

    # ── Fetch active tournament ──────────────────────────────────
    if args.fetch_active_tournament:
        mode = "subnet" if args.subnet else "full"
        print("=" * 50)
        print(f"ACTIVE TOURNAMENT ({mode})")
        print("=" * 50)
        try:
            t = fetch_active_tournament(args.server_url, subnet=args.subnet)
            print(f"  ID:               {t.get('id')}")
            print(f"  Status:           {t.get('status')}")
            print(f"  Task:             {t.get('task_name')}")
            print(f"  Network:          {t.get('network', 'N/A')}")
            print(f"  Subnet UID:       {t.get('subnet_uid', 'N/A')}")
            print()
            print(f"  Contest:          {_fmt_ts(t.get('contest_start_time'))}  →  {_fmt_ts(t.get('contest_end_time'))}")
            print(f"  Evaluation:       {_fmt_ts(t.get('evaluation_start_time'))}  →  {_fmt_ts(t.get('evaluation_end_time'))}")
            print(f"  Submit window:    {_fmt_ts(t.get('submit_window_start_time'))}")
            print(f"  Reward:           {_fmt_ts(t.get('reward_start_time'))}  →  {_fmt_ts(t.get('reward_end_time'))}")
            print()
            has_pe = t.get("has_public_eval", False)
            print(f"  Has public eval:  {has_pe}")
            if has_pe:
                print(f"  Eval phase:       {t.get('current_eval_phase') or 'N/A'}")
                print(f"  Public eval ends: {_fmt_ts(t.get('public_eval_end_time'))}")
                print(f"  Buffer hours:     {t.get('public_eval_buffer_hours', 'N/A')}")
            if not args.subnet:
                stats = t.get("statistics", {})
                if stats:
                    print()
                    print(f"  Agents:           {stats.get('total_agents', 'N/A')}")
                    print(f"  Evaluations:      {stats.get('total_evaluations', 'N/A')}")
                    print(f"  Validators:       {stats.get('active_validators', 'N/A')}")
        except Exception as e:
            print(f"  FAILED: {e}", file=sys.stderr)
            return 1
        print()

    # ── Download agent ───────────────────────────────────────────
    if args.download_agent:
        print("=" * 50)
        print("DOWNLOAD AGENT")
        print("=" * 50)
        try:
            dest = download_agent(
                args.server_url,
                args.api_key,
                agent_id,
                Path(args.output_dir),
            )
            size_kb = dest.stat().st_size / 1024
            print(f"  Saved to:  {dest}")
            print(f"  Size:      {size_kb:.1f} KB")
        except Exception as e:
            print(f"  FAILED: {e}", file=sys.stderr)
            return 1
        print()

    # ── Fetch leaderboard ────────────────────────────────────────
    if args.fetch_leaderboard:
        print("=" * 50)
        print("LEADERBOARD")
        print("=" * 50)
        try:
            result = fetch_leaderboard(
                args.server_url,
                tournament_id,
                phase=args.phase,
                limit=10,
            )
            lb_phase = result.get("phase", "N/A")
            available = result.get("available_phases", [])
            is_final = result.get("is_final", False)
            entries = result.get("entries", [])
            total = result.get("total", 0)

            print(f"  Phase:            {lb_phase}")
            print(f"  Available phases: {', '.join(available) if available else 'N/A'}")
            print(f"  Is final:         {is_final}")
            print(f"  Total ranked:     {total}")
            print()

            if entries:
                # Column header
                print(f"  {'Rank':<6} {'Score':>10}  {'Evals':>5}  Miner Hotkey")
                print(f"  {'─' * 6} {'─' * 10}  {'─' * 5}  {'─' * 48}")
                for e in entries:
                    rank = e.get("rank", "?")
                    score = e.get("aggregated_score")
                    n_evals = e.get("num_evaluations", 0)
                    hotkey = e.get("miner_hotkey", "?")
                    score_str = f"{score:.4f}" if score is not None else "N/A"
                    print(f"  {rank:<6} {score_str:>10}  {n_evals:>5}  {hotkey}")
            else:
                print("  No ranked entries yet.")
        except Exception as e:
            print(f"  FAILED: {e}", file=sys.stderr)
            return 1
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
