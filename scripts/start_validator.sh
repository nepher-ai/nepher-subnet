#!/bin/bash
# Start Nepher Validator
#
# Usage: ./scripts/start_validator.sh [--config PATH]

set -e

# Default config path
CONFIG_PATH="${1:-config/validator_config.yaml}"

# Check for config file
if [[ "$1" == "--config" ]]; then
    CONFIG_PATH="$2"
fi

if [ ! -f "$CONFIG_PATH" ]; then
    echo "Error: Configuration file not found: $CONFIG_PATH"
    echo ""
    echo "Please copy the example config and customize it:"
    echo "  cp config/validator_config.example.yaml config/validator_config.yaml"
    exit 1
fi

# Check for API key
if [ -z "$NEPHER_API_KEY" ]; then
    echo "Warning: NEPHER_API_KEY environment variable not set"
    echo "Make sure it's configured in your config file or environment"
fi

echo "=============================================="
echo "Starting Nepher Validator"
echo "Config: $CONFIG_PATH"
echo "=============================================="

# Run validator
python -m validator run --config "$CONFIG_PATH"

