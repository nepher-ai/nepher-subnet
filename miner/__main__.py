"""
Miner CLI entry point.

Usage:
    python -m miner submit --path ./my-agent --wallet-name miner --wallet-hotkey default
    python -m miner submit --path ./my-agent --config ./config/miner_config.yaml
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Optional

from nepher_core.utils.logging import setup_logging, get_logger
from nepher_core.config.loader import load_config
from nepher_core.config.models import MinerConfig
from miner.submit import (
    submit_agent,
    validate_agent_structure,
    list_active_tournaments,
    is_submittable,
    describe_stage,
)

logger = get_logger(__name__)


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        prog="nepher-miner",
        description="Nepher Miner - Submit trained agents to the tournament",
    )
    
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    # Submit command
    submit_parser = subparsers.add_parser(
        "submit",
        help="Submit an agent to the tournament",
    )
    submit_parser.add_argument(
        "--path",
        type=Path,
        required=True,
        help="Path to agent directory",
    )
    submit_parser.add_argument(
        "--config",
        type=Path,
        help="Path to miner config YAML file",
    )
    submit_parser.add_argument(
        "--wallet-name",
        type=str,
        help="Wallet name (default: miner)",
    )
    submit_parser.add_argument(
        "--wallet-hotkey",
        type=str,
        help="Hotkey name (default: default)",
    )
    submit_parser.add_argument(
        "--api-key",
        type=str,
        help="API key (or set NEPHER_API_KEY env var)",
    )
    submit_parser.add_argument(
        "--api-url",
        type=str,
        help="Tournament API URL",
    )
    submit_parser.add_argument(
        "--skip-validation",
        action="store_true",
        help="Skip agent structure validation",
    )
    submit_parser.add_argument(
        "--tournament-id",
        type=str,
        help=(
            "Target tournament ID. Required when multiple tournaments are "
            "active. When omitted with a single active tournament, it is "
            "auto-selected."
        ),
    )
    submit_parser.add_argument(
        "--all",
        action="store_true",
        dest="submit_all",
        help="Submit to every active tournament currently in its submit window",
    )

    # List-active command
    list_active_parser = subparsers.add_parser(
        "list-active",
        help="List active tournaments and whether they accept submissions",
    )
    for p in [list_active_parser]:
        p.add_argument("--config", type=Path, help="Path to miner config YAML file")
        p.add_argument("--api-key", type=str, help="API key (or set NEPHER_API_KEY)")
        p.add_argument("--api-url", type=str, help="Tournament API URL")

    # Validate command
    validate_parser = subparsers.add_parser(
        "validate",
        help="Validate agent structure without submitting",
    )
    validate_parser.add_argument(
        "--path",
        type=Path,
        required=True,
        help="Path to agent directory",
    )
    
    # Common arguments
    for subparser in [submit_parser, validate_parser, list_active_parser]:
        subparser.add_argument(
            "--verbose", "-v",
            action="store_true",
            help="Enable verbose output",
        )
    
    return parser.parse_args()


def _load_miner_config(args: argparse.Namespace) -> Optional[MinerConfig]:
    """Load the miner config file if one was provided."""
    if not getattr(args, "config", None):
        return None
    if not args.config.exists():
        raise FileNotFoundError(f"Config file not found: {args.config}")
    logger.info(f"Loading config from {args.config}")
    return load_config(args.config, MinerConfig)


def _resolve_credentials(args: argparse.Namespace):
    """Resolve (api_key, api_url, wallet_name, wallet_hotkey).

    Precedence: CLI args > config file > environment > defaults.
    Returns None if a required value (api_key) is missing.
    """
    try:
        config = _load_miner_config(args)
    except FileNotFoundError as e:
        logger.error(str(e))
        return None
    except ValueError as e:
        if "Environment variable" in str(e) and "is not set" in str(e):
            logger.error(f"Config error: {e}")
            logger.error("Set the environment variable or provide the value via CLI argument")
            logger.error("Example: export NEPHER_API_KEY=your_key")
            return None
        raise

    def resolve(cli_val, config_val, env_key=None, default=None):
        return cli_val or config_val or (os.environ.get(env_key) if env_key else None) or default

    api_key = resolve(args.api_key, config.api_key if config else None, "NEPHER_API_KEY")
    api_url = resolve(args.api_url, config.api_url if config else None, default="https://tournament-api.nepher.ai")
    wallet_name = resolve(
        getattr(args, "wallet_name", None), config.wallet.name if config else None, default="miner"
    )
    wallet_hotkey = resolve(
        getattr(args, "wallet_hotkey", None), config.wallet.hotkey if config else None, default="default"
    )

    if not api_key:
        logger.error("API key required. Use --api-key, config file, or set NEPHER_API_KEY")
        return None

    return api_key, api_url, wallet_name, wallet_hotkey


def _log_connection_error(api_url: str, e: Exception) -> None:
    error_str = str(e)
    if "ConnectError" in error_str or "ConnectionError" in error_str:
        logger.error(f"Connection failed to: {api_url}")
        logger.error("Possible causes:")
        logger.error("  - Server is not running")
        logger.error("  - Wrong port number")
        logger.error("  - Using https:// but server uses http://")
        logger.error("  - Firewall blocking the connection")
    else:
        logger.error(f"Submission failed: {e}")


async def _submit_to_one(args, creds, tournament_id: Optional[str]) -> bool:
    """Submit to a single tournament. Returns True on success."""
    api_key, api_url, wallet_name, wallet_hotkey = creds
    label = tournament_id or "(auto-selected)"
    try:
        await submit_agent(
            agent_path=args.path,
            wallet_name=wallet_name,
            wallet_hotkey=wallet_hotkey,
            api_key=api_key,
            api_url=api_url,
            tournament_id=tournament_id,
        )
        return True
    except Exception as e:
        logger.error(f"Submission to tournament {label} failed: {e}")
        _log_connection_error(api_url, e)
        return False


async def run_submit(args: argparse.Namespace) -> int:
    """Run the submit command (single, explicit, or --all targeting)."""
    creds = _resolve_credentials(args)
    if creds is None:
        return 1
    api_key, api_url, wallet_name, wallet_hotkey = creds

    # Validate agent structure first
    if not args.skip_validation:
        logger.info("Validating agent structure...")
        is_valid, errors = validate_agent_structure(args.path)
        if not is_valid:
            logger.error("Agent validation failed:")
            for error in errors:
                logger.error(f"  - {error}")
            return 1
        logger.info("Agent structure is valid")

    logger.info(f"Submitting with wallet: {wallet_name}/{wallet_hotkey}")

    # Mutually exclusive options
    if args.submit_all and args.tournament_id:
        logger.error("Use either --all or --tournament-id, not both")
        return 1

    # --all: submit to every active tournament in its submit window.
    if args.submit_all:
        try:
            tournaments = await list_active_tournaments(api_key, api_url)
        except Exception as e:
            logger.error(f"Failed to list active tournaments: {e}")
            _log_connection_error(api_url, e)
            return 1

        targets = [t for t in tournaments if is_submittable(t)]
        skipped = [t for t in tournaments if not is_submittable(t)]
        for t in skipped:
            logger.info(
                f"Skipping {t.id} ({t.task_name}) — stage '{describe_stage(t)}' "
                "is not accepting submissions"
            )
        if not targets:
            logger.error("No active tournaments are currently accepting submissions")
            return 1

        results = []
        for t in targets:
            logger.info(f"Submitting to {t.id} ({t.task_name})...")
            ok = await _submit_to_one(args, creds, str(t.id))
            results.append((t, ok))

        succeeded = [t for t, ok in results if ok]
        failed = [t for t, ok in results if not ok]
        logger.info(
            f"--all complete: {len(succeeded)} succeeded, {len(failed)} failed"
        )
        for t in failed:
            logger.error(f"  failed: {t.id} ({t.task_name})")
        return 0 if not failed else 1

    # Explicit target.
    if args.tournament_id:
        ok = await _submit_to_one(args, creds, args.tournament_id)
        return 0 if ok else 1

    # No explicit target: auto-select only when a single tournament is active.
    try:
        tournaments = await list_active_tournaments(api_key, api_url)
    except Exception as e:
        logger.error(f"Failed to list active tournaments: {e}")
        _log_connection_error(api_url, e)
        return 1

    if len(tournaments) > 1:
        logger.error(
            f"Multiple active tournaments ({len(tournaments)}). "
            "Re-run with --tournament-id <id> or --all. Active tournaments:"
        )
        for t in tournaments:
            submittable = "submittable" if is_submittable(t) else "not accepting submissions"
            logger.error(f"  - {t.id} ({t.task_name}) — {describe_stage(t)} [{submittable}]")
        return 1

    # 0 or 1 active tournament -> let the backend auto-select.
    ok = await _submit_to_one(args, creds, None)
    return 0 if ok else 1


async def run_list_active(args: argparse.Namespace) -> int:
    """List active tournaments and their submission status."""
    creds = _resolve_credentials(args)
    if creds is None:
        return 1
    api_key, api_url, _, _ = creds

    try:
        tournaments = await list_active_tournaments(api_key, api_url)
    except Exception as e:
        logger.error(f"Failed to list active tournaments: {e}")
        _log_connection_error(api_url, e)
        return 1

    if not tournaments:
        logger.info("No active tournaments")
        return 0

    logger.info(f"{len(tournaments)} active tournament(s):")
    for t in tournaments:
        submittable = "yes" if is_submittable(t) else "no"
        logger.info(
            f"  - {t.id} | {t.task_name} | stage={describe_stage(t)} | "
            f"accepts_submissions={submittable}"
        )
    return 0


def run_validate(args: argparse.Namespace) -> int:
    """Run the validate command."""
    logger.info(f"Validating agent at: {args.path}")
    
    is_valid, errors = validate_agent_structure(args.path)
    
    if is_valid:
        logger.info("✅ Agent structure is valid")
        return 0
    else:
        logger.error("❌ Agent validation failed:")
        for error in errors:
            logger.error(f"  - {error}")
        return 1


def main() -> int:
    """Main entry point."""
    args = parse_args()
    
    # Setup logging
    log_level = "DEBUG" if args.verbose else "INFO"
    setup_logging(level=log_level)
    
    # Run appropriate command
    if args.command == "submit":
        return asyncio.run(run_submit(args))
    elif args.command == "list-active":
        return asyncio.run(run_list_active(args))
    elif args.command == "validate":
        return run_validate(args)
    else:
        logger.error(f"Unknown command: {args.command}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

