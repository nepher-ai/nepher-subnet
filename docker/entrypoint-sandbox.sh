#!/bin/bash
# Nepher Sandbox Entrypoint
#
# Runs untrusted miner agent code in isolation with network filtering.
#
# Network security (transparent proxy):
#   1. Start squid transparent proxy (SNI-based domain whitelist)
#   2. iptables NAT redirects ALL outbound HTTPS → squid
#   3. iptables blocks ALL direct outbound (only squid can reach internet)
#   4. Drop NET_ADMIN capability so miner code cannot modify firewall
#   5. Run evaluation — only whitelisted domains are reachable
#
# Expected mounts:
#   /sandbox/agent   — extracted agent files (read-only)
#   /sandbox/config  — eval_config.yaml + task_config.yaml (read-only)
#   /sandbox/output  — evaluation_result.json written here
#   /sandbox/envs    — nepher environment cache (read-only)
#
# Environment variables:
#   TASK_MODULE  — name of the task module to install (e.g., spotwaypointnav)
#   EVAL_TIMEOUT — evaluation timeout in seconds (default: 3600)

set -e

export ISAACLAB_PATH=${ISAACLAB_PATH:-/isaac-lab}
export ISAACSIM_PATH=${ISAACSIM_PATH:-/isaac-sim}

# ── Helper: write error result and exit ────────────────────────
write_error() {
    local error_code="$1"
    local summary="$2"
    echo "[ERROR] ${summary}"
    echo "{\"score\": 0, \"metadata\": {\"error\": \"${error_code}\"}, \"summary\": \"${summary}\"}" \
        > /sandbox/output/evaluation_result.json
    exit 1
}

echo "=============================================="
echo "Nepher Sandbox Container"
echo "=============================================="
echo "Task Module: ${TASK_MODULE:-unknown}"
echo "Timeout:     ${EVAL_TIMEOUT:-3600}s"
echo "=============================================="

# ── GPU pre-flight ─────────────────────────────────────────────
echo "[SANDBOX] Checking GPU availability..."
if ! nvidia-smi &>/dev/null; then
    write_error "gpu_unavailable" "GPU not accessible in sandbox"
fi
echo "[SANDBOX] GPU OK:"
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader

# ── Network firewall (transparent proxy + iptables) ────────────
echo "[SANDBOX] Setting up network firewall..."

# Start squid transparent proxy
squid -f /etc/squid/squid.conf 2>/dev/null
sleep 1

if squid -f /etc/squid/squid.conf -k check 2>/dev/null; then
    echo "[SANDBOX] Squid proxy started"
else
    echo "[SANDBOX] WARNING: Squid failed to start, continuing without proxy"
fi

# iptables: redirect all outbound HTTPS/HTTP to squid intercept ports
# Skip redirect for squid's own connections (uid proxy) to avoid loop
iptables -t nat -A OUTPUT -m owner --uid-owner proxy -j RETURN
iptables -t nat -A OUTPUT -p tcp --dport 443 -j REDIRECT --to-port 3129
iptables -t nat -A OUTPUT -p tcp --dport 80  -j REDIRECT --to-port 3128

# iptables OUTPUT: allow only squid to reach the internet directly
iptables -A OUTPUT -o lo -j ACCEPT
iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
iptables -A OUTPUT -p udp --dport 53 -j ACCEPT
iptables -A OUTPUT -p tcp --dport 53 -j ACCEPT
# Block cloud metadata endpoint (critical — prevents IAM credential theft)
iptables -A OUTPUT -d 169.254.0.0/16 -j DROP
# Block private networks (prevent lateral movement)
iptables -A OUTPUT -d 10.0.0.0/8 -j DROP
iptables -A OUTPUT -d 172.16.0.0/12 -j DROP
iptables -A OUTPUT -d 192.168.0.0/16 -j DROP
# Allow squid (proxy user) to make direct connections
iptables -A OUTPUT -m owner --uid-owner proxy -j ACCEPT
# Drop everything else
iptables -A OUTPUT -j DROP

echo "[SANDBOX] Firewall active — only whitelisted domains reachable"

# ── Link environment cache ─────────────────────────────────────
if [ -d /sandbox/envs ] && [ "$(ls -A /sandbox/envs 2>/dev/null)" ]; then
    echo "[SANDBOX] Linking environment cache..."
    mkdir -p /root/.cache
    ln -sf /sandbox/envs /root/.cache/nepher
fi

# ── Clean stale results ────────────────────────────────────────
find /app -name "evaluation_result.json" -delete 2>/dev/null || true
rm -f /sandbox/output/evaluation_result.json

# ── Copy agent to writable location ───────────────────────────
echo "[SANDBOX] Copying agent to /app/agent..."
cp -r /sandbox/agent /app/agent

# ── Install task module ────────────────────────────────────────
TASK_MODULE=${TASK_MODULE:-""}
if [ -z "$TASK_MODULE" ]; then
    write_error "no_task_module" "TASK_MODULE not specified"
fi

SOURCE_PATH="/app/agent/source/${TASK_MODULE}"
if [ ! -d "$SOURCE_PATH" ]; then
    SOURCE_PATH=$(find /app/agent/source -mindepth 1 -maxdepth 1 -type d | head -1)
fi
if [ -z "$SOURCE_PATH" ] || [ ! -d "$SOURCE_PATH" ]; then
    write_error "module_not_found" "Task module source not found: ${TASK_MODULE}"
fi

echo "[SANDBOX] Installing task module from: ${SOURCE_PATH}"
${ISAACLAB_PATH}/isaaclab.sh -p -m pip install --no-build-isolation --no-deps -e "$SOURCE_PATH" 2>&1 || {
    write_error "install_failed" "Task module installation failed"
}

# ── Run evaluation (with NET_ADMIN dropped) ────────────────────
EVAL_SCRIPT="/app/eval-nav/scripts/evaluate.py"
EVAL_CONFIG="/sandbox/config/eval_config.yaml"
EVAL_TIMEOUT=${EVAL_TIMEOUT:-3600}

if [ ! -f "$EVAL_SCRIPT" ]; then
    write_error "eval_script_missing" "Evaluation script not found: ${EVAL_SCRIPT}"
fi
if [ ! -f "$EVAL_CONFIG" ]; then
    write_error "eval_config_missing" "Evaluation config not found: ${EVAL_CONFIG}"
fi

echo "[SANDBOX] Running evaluation (timeout: ${EVAL_TIMEOUT}s)..."
echo "[SANDBOX] Dropping NET_ADMIN capability — firewall is now immutable"

BOOTSTRAP="import multiprocessing, sys; multiprocessing.set_start_method('spawn', force=True); sys.argv = sys.argv[1:]; import runpy; runpy.run_path(sys.argv[0], run_name='__main__')"

# capsh --drop=cap_net_admin: miner code cannot modify iptables rules
capsh --drop=cap_net_admin -- -c "
    timeout ${EVAL_TIMEOUT} ${ISAACLAB_PATH}/isaaclab.sh -p -c \"${BOOTSTRAP}\" \
        \"${EVAL_SCRIPT}\" \
        --config \"${EVAL_CONFIG}\" \
        --headless 2>&1
"

EVAL_EXIT=$?

if [ $EVAL_EXIT -ne 0 ]; then
    echo "[SANDBOX] Evaluation exited with code: ${EVAL_EXIT}"
    if [ $EVAL_EXIT -eq 124 ]; then
        MSG="Evaluation timed out after ${EVAL_TIMEOUT}s"
    else
        MSG="Evaluation script failed with exit code ${EVAL_EXIT}"
    fi
    if [ ! -f /sandbox/output/evaluation_result.json ]; then
        echo "{\"score\": 0, \"metadata\": {\"error\": \"eval_failed\", \"exit_code\": ${EVAL_EXIT}}, \"summary\": \"${MSG}\"}" \
            > /sandbox/output/evaluation_result.json
    fi
fi

# ── Collect results ────────────────────────────────────────────
if [ ! -f /sandbox/output/evaluation_result.json ]; then
    FOUND=$(find /app -name "evaluation_result.json" -type f 2>/dev/null | head -1)
    if [ -n "$FOUND" ]; then
        echo "[SANDBOX] Found result at: ${FOUND}"
        cp "$FOUND" /sandbox/output/evaluation_result.json
    fi
fi

# Copy eval logs to output for the validator
LOG_DIR=$(find /app/logs -mindepth 2 -maxdepth 2 -type d -name "eval_run_*" 2>/dev/null | sort | tail -1)
if [ -n "$LOG_DIR" ]; then
    echo "[SANDBOX] Copying eval logs from: ${LOG_DIR}"
    cp -r "$LOG_DIR" /sandbox/output/eval_logs
fi

if [ ! -f /sandbox/output/evaluation_result.json ]; then
    write_error "no_result" "Evaluation completed but no result file generated"
fi

echo "[SANDBOX] Evaluation complete. Result:"
cat /sandbox/output/evaluation_result.json
echo ""
echo "[SANDBOX] Done."
