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
    # Load config from file if provided
    config: Optional[MinerConfig] = None
    if args.config:
        if not args.config.exists():
            logger.error(f"Config file not found: {args.config}")
            return 1
        logger.info(f"Loading config from {args.config}")
        try:
            config = load_config(args.config, MinerConfig)
        except ValueError as e:
            if "Environment variable" in str(e) and "is not set" in str(e):
                # Extract the variable name from the error message
                logger.error(f"Config error: {e}")
                logger.error("Set the environment variable or provide the value via CLI argument")
                logger.error("Example: export NEPHER_API_KEY=your_key")
                return 1
            raise
    
    # Resolve values: CLI args > config file > environment > defaults
    def resolve(cli_val, config_val, env_key=None, default=None):
        return cli_val or config_val or (os.environ.get(env_key) if env_key else None) or default
    
    api_key = resolve(args.api_key, config.api_key if config else None, "NEPHER_API_KEY")
    api_url = resolve(args.api_url, config.api_url if config else None, default="https://tournament.nepher.ai")
    wallet_name = resolve(args.wallet_name, config.wallet.name if config else None, default="miner")
    wallet_hotkey = resolve(args.wallet_hotkey, config.wallet.hotkey if config else None, default="default")
    
    if not api_key:
        logger.error("API key required. Use --api-key, config file, or set NEPHER_API_KEY")
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
    
    logger.info(f"Submitting with wallet: {wallet_name}/{wallet_hotkey}")
    
    try:
        await submit_agent(
            agent_path=args.path,
            wallet_name=wallet_name,
            wallet_hotkey=wallet_hotkey,
            api_key=api_key,
            api_url=api_url,
        )
        return 0
    except Exception as e:
        error_str = str(e)
        # Provide helpful message for connection errors
        if "ConnectError" in error_str or "ConnectionError" in error_str:
            logger.error(f"Connection failed to: {api_url}")
            logger.error("Possible causes:")
            logger.error("  - Server is not running")
            logger.error("  - Wrong port number")
            logger.error("  - Using https:// but server uses http://")
            logger.error("  - Firewall blocking the connection")
        else:
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

