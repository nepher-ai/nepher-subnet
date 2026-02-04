"""
Miner CLI entry point.

Usage:
    python -m miner submit --path ./my-agent --wallet-name miner --wallet-hotkey default
"""

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Optional

from nepher_core.utils.logging import setup_logging, get_logger
from miner.submit import submit_agent, validate_agent_structure

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
        "--wallet-name",
        type=str,
        default="miner",
        help="Wallet name (default: miner)",
    )
    submit_parser.add_argument(
        "--wallet-hotkey",
        type=str,
        default="default",
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
        default="https://tournament.nepher.ai",
        help="Tournament API URL",
    )
    submit_parser.add_argument(
        "--tournament-id",
        type=str,
        help="Tournament ID (uses active tournament if not specified)",
    )
    submit_parser.add_argument(
        "--agent-name",
        type=str,
        help="Optional agent name",
    )
    submit_parser.add_argument(
        "--skip-validation",
        action="store_true",
        help="Skip agent structure validation",
    )
    
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
    for subparser in [submit_parser, validate_parser]:
        subparser.add_argument(
            "--verbose", "-v",
            action="store_true",
            help="Enable verbose output",
        )
    
    return parser.parse_args()


async def run_submit(args: argparse.Namespace) -> int:
    """Run the submit command."""
    import os
    
    # Get API key from args or environment
    api_key = args.api_key or os.environ.get("NEPHER_API_KEY")
    if not api_key:
        logger.error("API key required. Use --api-key or set NEPHER_API_KEY")
        return 1
    
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
    
    try:
        await submit_agent(
            agent_path=args.path,
            wallet_name=args.wallet_name,
            wallet_hotkey=args.wallet_hotkey,
            api_key=api_key,
            api_url=args.api_url,
            tournament_id=args.tournament_id,
            agent_name=args.agent_name,
        )
        logger.info("Agent submitted successfully!")
        return 0
    except Exception as e:
        logger.error(f"Submission failed: {e}")
        return 1


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
    elif args.command == "validate":
        return run_validate(args)
    else:
        logger.error(f"Unknown command: {args.command}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

