#!/bin/bash
# ==============================================================================
# Local Agent Evaluation Script
#
# Evaluates an agent ZIP file locally using the validator Docker container.
# Bypasses the tournament API — extracts the ZIP directly, runs the evaluation
# inside Docker, and prints the result.
#
# Usage:
#   ./scripts/local_eval.sh <path-to-agent.zip>
#
# Example:
#   ./scripts/local_eval.sh 1773160710.444041.zip
#
# Prerequisites:
#   - Docker with NVIDIA Container Toolkit (nvidia-container-toolkit)
#   - The validator image built: docker compose build validator
# ==============================================================================

set -euo pipefail

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Colour

# ── Configuration ─────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
WORKSPACE="${PROJECT_DIR}/workspace"
AGENT_REGISTRY="${WORKSPACE}/agent_registry"
RESULT_FILE="${WORKSPACE}/evaluation_result.json"
EVAL_CONFIG="${WORKSPACE}/eval_config.yaml"
TASK_CONFIG="${WORKSPACE}/task_config.yaml"

# Docker image name (matches docker-compose.yaml service)
DOCKER_IMAGE="${DOCKER_IMAGE:-nepher-subnet-validator}"
CONTAINER_NAME="nepher-local-eval-$$"

# Nepher environment cache (host path)
NEPHER_CACHE="${NEPHER_CACHE:-${HOME}/.cache/nepher}"

# Evaluation timeout (seconds)
EVAL_TIMEOUT="${EVAL_TIMEOUT:-3600}"

# ── Helpers ───────────────────────────────────────────────────────────────────
log_info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
log_ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

cleanup() {
    log_info "Cleaning up..."
    docker rm -f "${CONTAINER_NAME}" 2>/dev/null || true
}
trap cleanup EXIT

usage() {
    echo "Usage: $0 <path-to-agent.zip>"
    echo ""
    echo "Environment variables:"
    echo "  DOCKER_IMAGE   Docker image name (default: nepher-subnet-validator)"
    echo "  EVAL_TIMEOUT   Evaluation timeout in seconds (default: 3600)"
    exit 1
}

# ── Argument parsing ──────────────────────────────────────────────────────────
if [[ $# -lt 1 ]]; then
    usage
fi

ZIP_FILE="$(realpath "$1")"

if [[ ! -f "${ZIP_FILE}" ]]; then
    log_error "ZIP file not found: ${ZIP_FILE}"
    exit 1
fi

# ── Pre-flight checks ────────────────────────────────────────────────────────
log_info "=== Nepher Local Agent Evaluation ==="
log_info "ZIP file: ${ZIP_FILE}"
log_info "Project: ${PROJECT_DIR}"

# Check Docker
if ! command -v docker &>/dev/null; then
    log_error "Docker is not installed."
    exit 1
fi

# Check GPU access
if ! docker run --rm --gpus all nvidia/cuda:12.1.0-base-ubuntu22.04 nvidia-smi &>/dev/null; then
    log_warn "GPU check failed — evaluation requires a GPU with NVIDIA Container Toolkit."
    log_warn "Continuing anyway (will fail inside container if GPU is truly unavailable)."
fi

# Check task_config.yaml exists
if [[ ! -f "${TASK_CONFIG}" ]]; then
    log_error "task_config.yaml not found at: ${TASK_CONFIG}"
    log_error "This file should already exist in workspace/. It defines the evaluation task."
    exit 1
fi

# ── Step 1: Check/build Docker image ─────────────────────────────────────────
log_info "Checking Docker image: ${DOCKER_IMAGE}"
if ! docker image inspect "${DOCKER_IMAGE}" &>/dev/null; then
    log_info "Image not found. Building with docker compose..."
    cd "${PROJECT_DIR}"
    docker compose build validator
    # docker compose names the image based on project+service
    DOCKER_IMAGE=$(docker compose images validator -q 2>/dev/null || echo "")
    if [[ -z "${DOCKER_IMAGE}" ]]; then
        DOCKER_IMAGE="nepher-subnet-validator"
    fi
    log_ok "Docker image built."
else
    log_ok "Docker image found."
fi

# ── Step 2: Prepare workspace ─────────────────────────────────────────────────
log_info "Preparing workspace..."

# Clean previous evaluation state (sudo needed — container runs as root,
# so .egg-info files are owned by root)
sudo rm -rf "${AGENT_REGISTRY}"
rm -f "${RESULT_FILE}" "${EVAL_CONFIG}"

# Extract agent ZIP to agent_registry/
mkdir -p "${AGENT_REGISTRY}"
unzip -q -o "${ZIP_FILE}" -d "${AGENT_REGISTRY}"
log_ok "Agent extracted to: ${AGENT_REGISTRY}"

# Verify expected structure
if [[ ! -d "${AGENT_REGISTRY}/best_policy" ]]; then
    log_error "Invalid agent: missing best_policy/ directory"
    exit 1
fi
if [[ ! -f "${AGENT_REGISTRY}/best_policy/best_policy.pt" ]]; then
    log_error "Invalid agent: missing best_policy/best_policy.pt"
    exit 1
fi
if [[ ! -d "${AGENT_REGISTRY}/source" ]]; then
    log_error "Invalid agent: missing source/ directory"
    exit 1
fi

# Detect task module from extracted source/
TASK_MODULE=$(ls "${AGENT_REGISTRY}/source/" | head -1)
log_info "Detected task module: ${TASK_MODULE}"

# Build eval_config.yaml with injected policy_path
# (mirrors AgentEvaluator._build_eval_config)
POLICY_PATH="/app/workspace/agent_registry/best_policy/best_policy.pt"
cp "${TASK_CONFIG}" "${EVAL_CONFIG}"

# Use python to properly inject policy_path into YAML
python3 -c "
import yaml, sys
with open('${EVAL_CONFIG}', 'r') as f:
    cfg = yaml.safe_load(f)
cfg['policy_path'] = '${POLICY_PATH}'
with open('${EVAL_CONFIG}', 'w') as f:
    yaml.dump(cfg, f, default_flow_style=False)
print('eval_config.yaml written with policy_path:', cfg['policy_path'])
"

log_ok "Eval config written: ${EVAL_CONFIG}"

# ── Step 3: Run evaluation in Docker ──────────────────────────────────────────
log_info "Starting evaluation in Docker container..."
log_info "Container: ${CONTAINER_NAME}"
log_info "Timeout: ${EVAL_TIMEOUT}s"
echo ""
echo "=============================================="
echo "  EVALUATION OUTPUT"
echo "=============================================="

# The evaluation command that runs inside the container:
#   1. Install the agent's task module (pip install -e)
#   2. Run evaluate.py with the config
#
# We override the entrypoint to run our custom evaluation commands
# instead of the full validator loop.
EVAL_CMD=$(cat <<'INNEREOF'
#!/bin/bash
set -e

export ISAACLAB_PATH=${ISAACLAB_PATH:-/isaac-lab}
PYTHON="${ISAACLAB_PATH}/isaaclab.sh -p"

echo "[LOCAL-EVAL] GPU check..."
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader || {
    echo "[ERROR] GPU not accessible"; exit 1;
}

echo "[LOCAL-EVAL] Installing agent task module..."
TASK_SOURCE=$(ls /app/workspace/agent_registry/source/ | head -1)
echo "[LOCAL-EVAL] Task module: ${TASK_SOURCE}"
${PYTHON} -m pip install -e "/app/workspace/agent_registry/source/${TASK_SOURCE}" 2>&1 | tail -5

# Verify module installed
if [ -f /app/workspace/agent_registry/scripts/list_envs.py ]; then
    echo "[LOCAL-EVAL] Verifying installation (list_envs.py)..."
    ${PYTHON} /app/workspace/agent_registry/scripts/list_envs.py 2>&1 || echo "[WARN] list_envs.py failed (non-fatal)"
fi

# Ensure environment scenes are cached
echo "[LOCAL-EVAL] Checking environment cache..."
MISSING_ENVS=$(${PYTHON} -c "
import yaml, os
with open('/app/workspace/eval_config.yaml') as f:
    cfg = yaml.safe_load(f)
missing = []
for s in cfg.get('env_scenes', []):
    env_id = s['env_id']
    cache_path = os.path.join('/root/.cache/nepher', env_id)
    if os.path.isdir(cache_path):
        print(f'[LOCAL-EVAL] Environment cached: {env_id}')
    else:
        missing.append(env_id)
if missing:
    print(f'[ERROR] Missing environments in cache: {missing}', flush=True)
    print(f'[ERROR] Mount the host cache with NEPHER_CACHE or download them first.')
    print(f'[ERROR] On the host: pip install nepher && nepher download {\" \".join(missing)}')
    raise SystemExit(1)
")
if [ $? -ne 0 ]; then
    exit 1
fi

echo "[LOCAL-EVAL] Running evaluation..."
EVAL_SCRIPT="/app/eval-nav/scripts/evaluate.py"
if [ ! -f "${EVAL_SCRIPT}" ]; then
    echo "[ERROR] evaluate.py not found at ${EVAL_SCRIPT}"
    exit 1
fi

# Run evaluation with multiprocessing spawn bootstrap (same as AgentEvaluator)
${PYTHON} -c "
import multiprocessing, sys
multiprocessing.set_start_method('spawn', force=True)
sys.argv = sys.argv[1:]
import runpy; runpy.run_path(sys.argv[0], run_name='__main__')
" "${EVAL_SCRIPT}" --config /app/workspace/eval_config.yaml --headless

echo "[LOCAL-EVAL] Evaluation script finished."

# Check for result file
RESULT="/app/workspace/evaluation_result.json"
RESULT_ALT="/app/eval-nav/evaluation_result.json"

if [ -f "${RESULT_ALT}" ] && [ ! -f "${RESULT}" ]; then
    mv "${RESULT_ALT}" "${RESULT}"
fi

if [ -f "${RESULT}" ]; then
    echo ""
    echo "=============================================="
    echo "  EVALUATION RESULT"
    echo "=============================================="
    cat "${RESULT}"
    echo ""
else
    echo "[ERROR] evaluation_result.json was not generated!"
    exit 1
fi
INNEREOF
)

# Write eval command to a temp script
EVAL_SCRIPT_HOST=$(mktemp /tmp/nepher_eval_XXXXXX.sh)
echo "${EVAL_CMD}" > "${EVAL_SCRIPT_HOST}"
chmod +x "${EVAL_SCRIPT_HOST}"

CACHE_MOUNT=""
if [[ -d "${NEPHER_CACHE}" ]]; then
    log_ok "Mounting host nepher cache: ${NEPHER_CACHE} -> /root/.cache/nepher"
    CACHE_MOUNT="-v ${NEPHER_CACHE}:/root/.cache/nepher"
else
    log_warn "No nepher cache found at ${NEPHER_CACHE}."
    log_warn "The container will attempt 'nepher download' for missing environments."
fi

docker run \
    --name "${CONTAINER_NAME}" \
    --gpus all \
    --rm \
    -e NVIDIA_VISIBLE_DEVICES=all \
    -e NVIDIA_DRIVER_CAPABILITIES=all \
    -e CUDA_MODULE_LOADING=LAZY \
    -e ACCEPT_EULA=Y \
    -e PRIVACY_CONSENT=Y \
    -e NEPHER_CACHE_DIR=/root/.cache/nepher \
    -e NEPHER_API_KEY="${NEPHER_API_KEY:-}" \
    -e ENVHUB_API_URL="${ENVHUB_API_URL:-https://envhub-api.nepher.ai}" \
    -v "${WORKSPACE}:/app/workspace" \
    -v "${EVAL_SCRIPT_HOST}:/tmp/run_eval.sh:ro" \
    ${CACHE_MOUNT} \
    --entrypoint /bin/bash \
    "${DOCKER_IMAGE}" \
    /tmp/run_eval.sh

EVAL_EXIT=$?
rm -f "${EVAL_SCRIPT_HOST}"

echo ""
echo "=============================================="

# ── Step 4: Show results ─────────────────────────────────────────────────────
if [[ ${EVAL_EXIT} -ne 0 ]]; then
    log_error "Evaluation failed with exit code: ${EVAL_EXIT}"
    exit ${EVAL_EXIT}
fi

if [[ -f "${RESULT_FILE}" ]]; then
    log_ok "Evaluation completed successfully!"
    echo ""
    log_info "Result file: ${RESULT_FILE}"
    echo ""

    # Pretty-print the score
    SCORE=$(python3 -c "import json; print(json.load(open('${RESULT_FILE}'))['score'])" 2>/dev/null || echo "N/A")
    echo -e "${GREEN}  Score: ${SCORE}${NC}"
    echo ""

    # Show full result
    log_info "Full result:"
    python3 -m json.tool "${RESULT_FILE}" 2>/dev/null || cat "${RESULT_FILE}"
else
    log_error "evaluation_result.json not found after evaluation."
    log_error "Check container output above for errors."
    exit 1
fi
