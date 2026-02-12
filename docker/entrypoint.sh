#!/bin/bash
# Nepher Validator Entrypoint Script

set -e

# Environment setup
export ISAACLAB_PATH=${ISAACLAB_PATH:-/workspace/IsaacLab}
export ISAACSIM_PATH=${ISAACSIM_PATH:-/isaac-sim}

echo "=============================================="
echo "Nepher Validator Container Starting"
echo "=============================================="
echo "Isaac Lab: ${ISAACLAB_PATH}"
echo "Isaac Sim: ${ISAACSIM_PATH}"
echo "=============================================="

# Run the validator using Isaac Lab's Python
exec ${ISAACLAB_PATH}/isaaclab.sh -p -m validator "$@"
