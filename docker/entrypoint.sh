#!/bin/bash
# Nepher Validator Entrypoint Script
# Lightweight orchestrator — spawns sandbox containers for evaluation.

set -e

echo "=============================================="
echo "Nepher Validator Container Starting"
echo "=============================================="

# ── Docker socket check ──────────────────────────────────────
echo "[PRE-FLIGHT] Checking Docker socket access..."

if [ ! -S /var/run/docker.sock ]; then
    echo "[ERROR] Docker socket not found at /var/run/docker.sock"
    echo "        Mount it with: -v /var/run/docker.sock:/var/run/docker.sock"
    exit 1
fi

if ! docker info &>/dev/null; then
    echo "[ERROR] Cannot communicate with Docker daemon."
    echo "        Ensure the Docker socket is mounted and accessible."
    exit 1
fi

echo "[PRE-FLIGHT] Docker daemon OK"
docker version --format '  Server: {{.Server.Version}}  Client: {{.Client.Version}}'

# ── Verify / auto-build sandbox image ─────────────────────────
SANDBOX_IMAGE=${SANDBOX_IMAGE:-nepher-sandbox:latest}
echo "[PRE-FLIGHT] Checking sandbox image: ${SANDBOX_IMAGE}"

if ! docker image inspect "${SANDBOX_IMAGE}" &>/dev/null; then
    echo "[PRE-FLIGHT] Sandbox image '${SANDBOX_IMAGE}' not found — building automatically..."

    # The project root is mounted at /app/project (read-only).
    BUILD_CONTEXT="/app/project"
    DOCKERFILE="${BUILD_CONTEXT}/docker/Dockerfile.sandbox"

    if [ ! -f "${DOCKERFILE}" ]; then
        echo "[ERROR] Cannot auto-build: ${DOCKERFILE} not found."
        echo "        Build manually with: docker build -f docker/Dockerfile.sandbox -t ${SANDBOX_IMAGE} ."
        exit 1
    fi

    # Pass through the EVAL_REPO_URL build arg if set
    BUILD_ARGS=""
    if [ -n "${EVAL_REPO_URL}" ]; then
        BUILD_ARGS="--build-arg EVAL_REPO_URL=${EVAL_REPO_URL}"
    fi

    if docker build ${BUILD_ARGS} -f "${DOCKERFILE}" -t "${SANDBOX_IMAGE}" "${BUILD_CONTEXT}"; then
        echo "[PRE-FLIGHT] Sandbox image built successfully."
    else
        echo "[ERROR] Failed to build sandbox image."
        echo "        Try manually: docker build -f docker/Dockerfile.sandbox -t ${SANDBOX_IMAGE} ."
        exit 1
    fi
fi

echo "=============================================="
echo "[INFO] Sandbox image: ${SANDBOX_IMAGE}"
echo "[INFO] Starting validator orchestrator..."
echo "=============================================="

# Run the validator
exec python -m validator "$@"
