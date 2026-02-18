#!/bin/bash
# Nepher Validator Entrypoint Script

set -e

# Environment setup
export ISAACLAB_PATH=${ISAACLAB_PATH:-/isaac-lab}
export ISAACSIM_PATH=${ISAACSIM_PATH:-/isaac-sim}

echo "=============================================="
echo "Nepher Validator Container Starting"
echo "=============================================="
echo "Isaac Lab: ${ISAACLAB_PATH}"
echo "Isaac Sim: ${ISAACSIM_PATH}"
echo "=============================================="

# ── Detect run mode from CLI args ────────────────────────────
# When --mode cpu is passed, GPU / CUDA checks are not required.
SKIP_GPU_CHECK=false
case " $* " in
    *" --mode cpu "*) SKIP_GPU_CHECK=true ;;
esac

if [ "$SKIP_GPU_CHECK" = "true" ]; then
    echo "[INFO] Running in CPU mode — skipping GPU pre-flight checks"
else
    # ── GPU pre-flight check ─────────────────────────────────
    echo "[PRE-FLIGHT] Checking GPU / CUDA availability..."

    if ! command -v nvidia-smi &>/dev/null; then
        echo "[ERROR] nvidia-smi not found inside the container."
        echo "        The NVIDIA Container Toolkit may not be installed on the host."
        echo "        See: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html"
        exit 1
    fi

    if ! nvidia-smi &>/dev/null; then
        echo "[ERROR] nvidia-smi failed — the GPU is not accessible from this container."
        echo ""
        echo "  Possible causes:"
        echo "    1. NVIDIA Container Toolkit is not installed on the host."
        echo "    2. Docker was not restarted after installing the toolkit."
        echo "       → sudo nvidia-ctk runtime configure --runtime=docker"
        echo "       → sudo systemctl restart docker"
        echo "    3. The host machine does not have a supported NVIDIA GPU."
        echo ""
        echo "  Quick test from the host:"
        echo "    docker run --rm --gpus all nvidia/cuda:12.1.0-base-ubuntu22.04 nvidia-smi"
        exit 1
    fi

    echo "[PRE-FLIGHT] nvidia-smi OK:"
    nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader
    echo "----------------------------------------------"

    # Verify CUDA is visible to PyTorch
    CUDA_CHECK=$(${ISAACLAB_PATH}/isaaclab.sh -p -c "import torch; print('cuda_available=' + str(torch.cuda.is_available()))" 2>&1) || true
    if echo "${CUDA_CHECK}" | grep -q "cuda_available=True"; then
        echo "[PRE-FLIGHT] PyTorch CUDA: available ✓"
    else
        echo "[ERROR] PyTorch cannot access CUDA inside this container."
        echo "        nvidia-smi works, but torch.cuda.is_available() returned False."
        echo ""
        echo "  This usually means the CUDA driver version on the host is too old"
        echo "  for the CUDA toolkit inside the container, or the NVIDIA Container"
        echo "  Toolkit is not forwarding GPU devices correctly."
        echo ""
        echo "  Host driver info:"
        nvidia-smi --query-gpu=driver_version --format=csv,noheader
        echo ""
        echo "  Container CUDA version: ${CUDA_VERSION:-unknown}"
        exit 1
    fi
fi

echo "[INFO] Using python from: ${ISAACLAB_PATH}/isaaclab.sh -p"

# Run the validator using Isaac Lab's Python
exec ${ISAACLAB_PATH}/isaaclab.sh -p -m validator "$@"
