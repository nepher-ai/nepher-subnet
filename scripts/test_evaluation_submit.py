#!/usr/bin/env python3
"""
Test script: send in-progress signal, submit evaluation result, fetch unevaluated
agents, or fetch winner hotkey for a tournament.

Use this to test the evaluation flow (in-progress → submit) with a given tournament_id
and agent_id. Requires validator API key and wallet for signing.

Usage:
  # In-progress only
  python scripts/test_evaluation_submit.py \\
    --tournament-id <UUID> --agent-id <UUID> \\
    --in-progress --api-key <KEY> --wallet-name X --wallet-hotkey Y

  # Submit result only
  python scripts/test_evaluation_submit.py \\
    --tournament-id <UUID> --agent-id <UUID> \\
    --submit --score 0.85 --api-key <KEY> --wallet-name X --wallet-hotkey Y

  # Both: in-progress then submit (full test flow)
  python scripts/test_evaluation_submit.py \\
    --tournament-id <UUID> --agent-id <UUID> \\
    --in-progress --submit --score 0.9 \\
    --api-key <KEY> --wallet-name X --wallet-hotkey Y --server-url http://localhost:8003

  # Clear in-progress (omit --agent-id)
  python scripts/test_evaluation_submit.py \\
    --tournament-id <UUID> --in-progress --api-key <KEY> --wallet-name X --wallet-hotkey Y

  # Fetch unevaluated agents for a validator (no wallet needed)
  python scripts/test_evaluation_submit.py \\
    --tournament-id <UUID> --fetch-unevaluated \\
    --api-key <KEY> --validator-hotkey-ss58 <SS58_ADDRESS>

  # Fetch unevaluated agents (wallet can also be used to derive the hotkey)
  python scripts/test_evaluation_submit.py \\
    --tournament-id <UUID> --fetch-unevaluated \\
    --api-key <KEY> --wallet-name X --wallet-hotkey Y

  # Fetch winner hotkey for a tournament (public, no API key or wallet needed)
  python scripts/test_evaluation_submit.py \\
    --tournament-id <UUID> --fetch-winner --server-url http://localhost:8003
"""

import argparse
import hashlib
import json
import sys
import time
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


def _validate_uuid(value: str, name: str) -> str:
    try:
        UUID(value)
        return value
    except ValueError:
        raise ValueError(f"Invalid {name}: must be a valid UUID")


def create_eval_info(
    hotkey: str,
    tournament_id: str,
    agent_id: str,
    timestamp: int,
    log_hash: Optional[str] = None,
) -> str:
    """eval_info string for signing: hotkey:tournament_id:agent_id:timestamp[:log_hash]."""
    base = f"{hotkey}:{tournament_id}:{agent_id}:{timestamp}"
    if log_hash:
        return f"{base}:{log_hash}"
    return base


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
            try:
                detail = response.json().get("detail", response.text)
            except Exception:
                detail = response.text
            raise RuntimeError(f"In-progress failed: {detail}")
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
            try:
                detail = response.json().get("detail", response.text)
            except Exception:
                detail = response.text
            raise RuntimeError(f"Verify failed: {detail}")
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
            try:
                detail = response.json().get("detail", response.text)
            except Exception:
                detail = response.text
            raise RuntimeError(f"Submit failed: {detail}")
        return response.json()


def fetch_unevaluated_agents(
    server_url: str,
    api_key: str,
    tournament_id: str,
    validator_hotkey: str,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
) -> dict:
    """GET /api/v1/agents/list/unevaluated"""
    with httpx.Client(timeout=30) as client:
        params = {
            "tournament_id": tournament_id,
            "validator_hotkey": validator_hotkey,
        }
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
            try:
                detail = response.json().get("detail", response.text)
            except Exception:
                detail = response.text
            raise RuntimeError(f"Fetch unevaluated agents failed ({response.status_code}): {detail}")
        return response.json()


def fetch_winner(
    server_url: str,
    tournament_id: str,
) -> dict:
    """GET /api/v1/tournaments/{tournament_id}/winner-hotkey"""
    with httpx.Client(timeout=30) as client:
        response = client.get(
            f"{server_url}/api/v1/tournaments/{tournament_id}/winner-hotkey",
        )
        if response.status_code != 200:
            try:
                detail = response.json().get("detail", response.text)
            except Exception:
                detail = response.text
            raise RuntimeError(f"Fetch winner failed ({response.status_code}): {detail}")
        return response.json()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Test script: send in-progress and/or submit evaluation for a tournament",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--tournament-id", "-t", required=True, help="Tournament ID (UUID)")
    parser.add_argument(
        "--agent-id", "-a",
        help="Agent ID (UUID). Required for --submit; optional for --in-progress (omit to clear in-progress).",
    )
    parser.add_argument(
        "--in-progress",
        action="store_true",
        help="Send in-progress signal (use with --agent-id to set, without to clear)",
    )
    parser.add_argument(
        "--submit",
        action="store_true",
        help="Submit evaluation result (after verify step)",
    )
    parser.add_argument(
        "--fetch-unevaluated",
        action="store_true",
        help="Fetch list of agents not yet evaluated by this validator",
    )
    parser.add_argument(
        "--fetch-winner",
        action="store_true",
        help="Fetch winner hotkey for a tournament (public endpoint, no API key or wallet needed)",
    )
    parser.add_argument("--score", "-s", type=float, default=0.0, help="Score for submit (default: 0.0)")
    parser.add_argument("--summary", help="Optional summary text for submit")
    parser.add_argument("--metadata", help="Optional JSON object for submit metadata")
    parser.add_argument("--log-file", "-l", help="Optional log file (zip) for submit")
    parser.add_argument("--api-key", help="Validator API key (required for --in-progress, --submit, --fetch-unevaluated)")
    parser.add_argument("--server-url", default=DEFAULT_SERVER_URL, help=f"Backend URL (default: {DEFAULT_SERVER_URL})")
    parser.add_argument("--wallet-name", help="Bittensor wallet name (required for --in-progress / --submit)")
    parser.add_argument("--wallet-hotkey", help="Bittensor hotkey name (required for --in-progress / --submit)")
    parser.add_argument(
        "--validator-hotkey-ss58",
        help="Validator SS58 hotkey address. Use instead of --wallet-name/--wallet-hotkey for read-only actions like --fetch-unevaluated.",
    )

    args = parser.parse_args()

    if not args.in_progress and not args.submit and not args.fetch_unevaluated and not args.fetch_winner:
        parser.error("Specify at least one of --in-progress, --submit, --fetch-unevaluated, or --fetch-winner")
    if args.submit and not args.agent_id:
        parser.error("--agent-id is required for --submit")

    # Wallet is required for --in-progress and --submit (signing needed)
    needs_wallet = args.in_progress or args.submit
    if needs_wallet and (not args.wallet_name or not args.wallet_hotkey):
        parser.error("--wallet-name and --wallet-hotkey are required for --in-progress / --submit")

    # For fetch-unevaluated, either wallet or --validator-hotkey-ss58 must be provided
    needs_hotkey = args.fetch_unevaluated
    if needs_hotkey and not needs_wallet and not args.validator_hotkey_ss58 and not (args.wallet_name and args.wallet_hotkey):
        parser.error("Provide --validator-hotkey-ss58 or --wallet-name/--wallet-hotkey for --fetch-unevaluated")

    # --api-key is required for everything except --fetch-winner
    needs_api_key = args.in_progress or args.submit or args.fetch_unevaluated
    if needs_api_key and not args.api_key:
        parser.error("--api-key is required for --in-progress, --submit, and --fetch-unevaluated")

    if args.in_progress and not args.agent_id and not args.submit:
        # Clear in-progress: agent_id can be None
        pass
    elif args.agent_id:
        try:
            _validate_uuid(args.agent_id, "agent_id")
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

    tournament_id = _validate_uuid(args.tournament_id, "tournament_id")
    agent_id = args.agent_id  # None when clearing in-progress

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

    print(f"Tournament: {tournament_id}")
    print(f"Agent:      {agent_id or '(N/A)'}")
    print(f"Server:     {args.server_url}")
    print()

    # Resolve hotkey: from wallet if available, otherwise from --validator-hotkey-ss58
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

    # Prepare signing data only when wallet is available (in-progress / submit)
    timestamp = None
    public_key = None
    signature_in_progress = None
    eval_info_in_progress = None

    if wallet:
        timestamp = int(time.time())
        # In-progress uses eval_info without log_hash (agent_id empty string when clearing)
        eval_info_in_progress = create_eval_info(hotkey_ss58, tournament_id, agent_id or "", timestamp)
        public_key, signature_in_progress = sign_eval_info(wallet, eval_info_in_progress)

    if args.in_progress:
        print("Sending in-progress...")
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
            print("  OK:", result.get("message", result))
        except Exception as e:
            print(f"  Failed: {e}", file=sys.stderr)
            return 1
        print()

    if args.submit:
        # Submit uses eval_info with optional log_hash for verify (wallet guaranteed by validation above)
        eval_info_submit = create_eval_info(hotkey_ss58, tournament_id, agent_id, timestamp, log_hash)
        public_key2, signature_submit = sign_eval_info(wallet, eval_info_submit)

        print("Requesting upload token...")
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
            print(f"  Failed: {e}", file=sys.stderr)
            return 1
        upload_token = token_resp["upload_token"]
        print("  OK")
        if token_resp.get("existing_status"):
            print(f"  Existing status: {token_resp['existing_status']}")

        print("Submitting evaluation...")
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
            print("  OK")
            print(f"  evaluation_id: {result['evaluation_id']}")
            print(f"  status:        {result['status']}")
            print(f"  score:         {result.get('score')}")
        except Exception as e:
            print(f"  Failed: {e}", file=sys.stderr)
            return 1

    if args.fetch_unevaluated:
        print("Fetching unevaluated agents...")
        try:
            result = fetch_unevaluated_agents(
                args.server_url,
                args.api_key,
                tournament_id,
                hotkey_ss58,
            )
            agents = result.get("agents", [])
            total = result.get("total", 0)
            print(f"  Total unevaluated agents: {total}")
            if agents:
                print()
                for i, agent in enumerate(agents, 1):
                    print(f"  [{i}] Agent ID:      {agent['id']}")
                    print(f"      Miner Hotkey:  {agent.get('miner_hotkey', 'N/A')}")
                    print(f"      Task:          {agent.get('task_name', 'N/A')}")
                    print(f"      Score:         {agent.get('score', 'N/A')}")
                    print(f"      Status:        {agent.get('status', 'N/A')}")
                    print(f"      Uploaded At:   {agent.get('uploaded_at', 'N/A')}")
                    print()
            else:
                print("  No unevaluated agents found (all agents have been evaluated by this validator).")
        except Exception as e:
            print(f"  Failed: {e}", file=sys.stderr)
            return 1

    if args.fetch_winner:
        print("Fetching winner hotkey...")
        try:
            result = fetch_winner(
                args.server_url,
                tournament_id,
            )
            print(f"  Tournament ID:    {result.get('tournament_id')}")
            print(f"  Winner Approved:  {result.get('winner_approved')}")
            print(f"  Winner Hotkey:    {result.get('winner_hotkey') or 'N/A'}")
            print(f"  Winner Agent ID:  {result.get('winner_agent_id') or 'N/A'}")
            print(f"  Winner Score:     {result.get('winner_score') if result.get('winner_score') is not None else 'N/A'}")
        except Exception as e:
            print(f"  Failed: {e}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
