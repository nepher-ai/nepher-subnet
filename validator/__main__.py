"""
Validator CLI entry point.

Usage:
    python -m validator run --config ./config/validator_config.yaml
"""

import argparse
import asyncio
import sys
from pathlib import Path

from nepher_core.utils.logging import setup_logging, get_logger
from validator.main import run_validator

logger = get_logger(__name__)


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        prog="nepher-validator",
        description="Nepher Validator - Evaluate agents and set weights",
    )
    
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    # Run command
    run_parser = subparsers.add_parser(
        "run",
        help="Run the validator",
    )
    run_parser.add_argument(
        "--config", "-c",
        type=Path,
        required=True,
        help="Path to validator configuration file",
    )
    run_parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose output",
    )
    run_parser.add_argument(
        "--json-logs",
        action="store_true",
        help="Use JSON log format (for production)",
    )
    run_parser.add_argument(
        "--log-file",
        type=str,
        help="Path to log file",
    )
    run_parser.add_argument(
        "--mode",
        type=str,
        choices=["cpu", "gpu"],
        default=None,
        help="Run mode: gpu (default, full behaviour) or cpu (weights/burn only)",
    )
    
    return parser.parse_args()


async def async_main(args: argparse.Namespace) -> int:
    """Async main entry point."""
    if args.command == "run":
        try:
            await run_validator(args.config, mode=args.mode)
            return 0
        except KeyboardInterrupt:
            logger.info("Validator stopped by user")
            return 0
        except Exception as e:
            logger.error(f"Validator failed: {e}", exc_info=True)
            return 1
    else:
        logger.error(f"Unknown command: {args.command}")
        return 1


def main() -> int:
    """Main entry point."""
    args = parse_args()
    
    # Setup logging
    log_level = "DEBUG" if args.verbose else "INFO"
    setup_logging(
        level=log_level,
        json_format=getattr(args, "json_logs", False),
        log_file=getattr(args, "log_file", None),
    )
    
    # Run async main
    return asyncio.run(async_main(args))


if __name__ == "__main__":
    sys.exit(main())

