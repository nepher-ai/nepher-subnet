#!/usr/bin/env bash
# Smoke-test the Isaac Sim 6.1 / Isaac Lab 3.0 sandbox image on a GPU host.
#
# Prerequisites:
#   - Docker + NVIDIA Container Toolkit
#   - NGC access to pull nvcr.io/nvidia/isaac-sim:6.1.0
#   - Host NVIDIA driver 580+ (595.58.03 recommended for Isaac Sim 6.1)
#
# Usage (from repo root):
#   ./scripts/smoke_test_sandbox_isaac61.sh
#   SANDBOX_IMAGE=nepher-sandbox:isaacsim6.1-lab3.0 ./scripts/smoke_test_sandbox_isaac61.sh

set -euo pipefail

SANDBOX_IMAGE="${SANDBOX_IMAGE:-nepher-sandbox:isaacsim6.1-lab3.0}"
EVAL_REPO_URL="${EVAL_REPO_URL:-https://github.com/nepher-ai/eval-nav.git}"
EVAL_REPO_REF="${EVAL_REPO_REF:-}"

echo "=== Host GPU / driver ==="
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader

echo "=== Building sandbox image: ${SANDBOX_IMAGE} ==="
BUILD_ARGS=(--build-arg "EVAL_REPO_URL=${EVAL_REPO_URL}")
if [ -n "${EVAL_REPO_REF}" ]; then
  BUILD_ARGS+=(--build-arg "EVAL_REPO_REF=${EVAL_REPO_REF}")
fi
docker build "${BUILD_ARGS[@]}" \
  -f docker/Dockerfile.sandbox \
  -t "${SANDBOX_IMAGE}" \
  .

echo "=== Import / CUDA smoke test inside image ==="
docker run --rm --gpus all --entrypoint bash "${SANDBOX_IMAGE}" -lc '
  set -e
  export ISAACLAB_PATH=${ISAACLAB_PATH:-/isaac-lab}
  ${ISAACLAB_PATH}/isaaclab.sh -p -c "
import isaaclab, torch, numpy
print(\"isaaclab\", getattr(isaaclab, \"__version__\", \"unknown\"))
print(\"torch\", torch.__version__, \"cuda\", torch.cuda.is_available())
print(\"numpy\", numpy.__version__)
if not torch.cuda.is_available():
    raise SystemExit(\"CUDA not available inside sandbox\")
"
'

echo "=== Image size ==="
docker image inspect "${SANDBOX_IMAGE}" --format '{{.Size}}'

cat <<'EOF'
=== Next manual checks (not automated here) ===
1. Run one full sandbox evaluation with a Lab 3.0-ported agent; compare score/camera vs 5.1 baseline.
2. Run one evaluation with a Lab 2.3-era agent; confirm evaluation_result.json error (not missing result).
EOF

echo "Smoke test complete."
