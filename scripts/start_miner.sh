#!/bin/bash
# Start Nepher Miner Submission
#
# Usage: ./scripts/start_miner.sh --path ./my-agent [options]

set -e

# Help message
if [ "$1" == "--help" ] || [ "$1" == "-h" ]; then
    echo "Nepher Miner - Agent Submission"
    echo ""
    echo "Usage: $0 --path <agent_path> [options]"
    echo ""
    echo "Options:"
    echo "  --path          Path to agent directory (required)"
    echo "  --wallet-name   Wallet name (default: miner)"
    echo "  --wallet-hotkey Hotkey name (default: default)"
    echo "  --api-key       API key (or set NEPHER_API_KEY env var)"
    echo ""
    echo "Example:"
    echo "  $0 --path ./my-agent --wallet-name miner"
    exit 0
fi

# Check for API key
if [ -z "$NEPHER_API_KEY" ]; then
    echo "Warning: NEPHER_API_KEY not set, you'll need to provide --api-key"
fi

echo "=============================================="
echo "Nepher Miner - Submitting Agent"
echo "=============================================="

# Run miner with all arguments
python -m miner submit "$@"

