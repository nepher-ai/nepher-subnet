#!/bin/bash
# Nepher Sandbox Entrypoint
#
# Runs agent evaluation in an isolated environment with strict network firewall.
#
# Security model:
#   - iptables firewall blocks ALL outbound traffic by default
#   - Allows ONLY: Isaac Sim CDN (Omniverse S3), pip install (PyPI)
#   - Blocks: cloud metadata, private networks, everything else
#
# Expected mounts:
#   /sandbox/agent   — extracted agent files (read-only)
#   /sandbox/config  — eval_config.yaml + task_config.yaml (read-only)
#   /sandbox/output  — evaluation_result.json written here
#   /sandbox/envs    — nepher environment cache (read-only)
#
# Environment variables:
#   TASK_MODULE      — name of the task module to install (e.g., spotwaypointnav)
#   EVAL_TIMEOUT     — evaluation timeout in seconds (default: 3600)

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
echo "Nepher Sandbox Container Starting"
echo "=============================================="
echo "Task Module: ${TASK_MODULE:-unknown}"
echo "Timeout:     ${EVAL_TIMEOUT:-3600}s"
echo "=============================================="

# ── GPU pre-flight check ─────────────────────────────────────
echo "[SANDBOX] Checking GPU availability..."
if ! nvidia-smi &>/dev/null; then
    write_error "gpu_unavailable" "GPU not accessible in sandbox"
fi
echo "[SANDBOX] GPU OK:"
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader

# ── Network firewall (whitelist) ──────────────────────────────
# STRICT whitelist: block everything, allow only what's needed.
# This MUST run before any untrusted miner code executes.
#
# Allowed destinations:
#   1. omniverse-content-production.s3-us-west-2.amazonaws.com (Isaac Sim USD assets)
#      — uses AWS S3 CIDR ranges because individual IPs rotate across CDN edges
#   2. pypi.org + files.pythonhosted.org (pip install for agent dependencies)
#      — uses Fastly CDN CIDR + runtime DNS resolution
#
# Blocked:
#   - 169.254.0.0/16 (cloud metadata — AWS/GCP/Azure credential theft)
#   - 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16 (private networks)
#   - Everything else

echo "[SANDBOX] Setting up network firewall (whitelist mode)..."

setup_firewall() {
    # Flush existing rules
    iptables -F OUTPUT 2>/dev/null || true
    iptables -F INPUT 2>/dev/null || true

    # ── 1. Allow loopback (always needed) ──
    iptables -A OUTPUT -o lo -j ACCEPT

    # ── 2. Allow already-established connections (MUST be early) ──
    # Once a whitelisted outbound connection is made, all subsequent packets
    # (ACKs, data) in that session are allowed without re-matching rules.
    iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT

    # ── 3. Allow DNS (needed to resolve whitelisted domains) ──
    iptables -A OUTPUT -p udp --dport 53 -j ACCEPT
    iptables -A OUTPUT -p tcp --dport 53 -j ACCEPT

    # ── 4. BLOCK dangerous destinations (before any allows) ──
    # Cloud metadata endpoints — can leak IAM credentials
    iptables -A OUTPUT -d 169.254.0.0/16 -j DROP
    # Private networks — prevent lateral movement
    iptables -A OUTPUT -d 10.0.0.0/8 -j DROP
    iptables -A OUTPUT -d 172.16.0.0/12 -j DROP
    iptables -A OUTPUT -d 192.168.0.0/16 -j DROP
    echo "[SANDBOX]   Blocked: metadata + private networks"

    # ── 5. ALLOW: AWS S3 us-west-2 (Isaac Sim USD assets) ──
    # omniverse-content-production.s3-us-west-2.amazonaws.com resolves to
    # IPs across multiple S3 prefixes that rotate. Using /16 CIDR ranges
    # from AWS's published IP ranges to cover all edge nodes.
    #
    # Verified from: https://ip-ranges.amazonaws.com/ip-ranges.json
    # (service=S3, region=us-west-2)
    local S3_CIDRS=(
        "3.5.0.0/19"         # 3.5.0.0 – 3.5.31.255
        "3.5.64.0/19"        # 3.5.64.0 – 3.5.95.255  (covers 3.5.79/80/84.x)
        "52.92.128.0/17"     # 52.92.128.0 – 52.92.255.255
        "52.218.128.0/17"    # 52.218.128.0 – 52.218.255.255
        "16.182.0.0/15"      # 16.182.0.0 – 16.183.255.255
    )
    for cidr in "${S3_CIDRS[@]}"; do
        iptables -A OUTPUT -d "$cidr" -p tcp --dport 443 -j ACCEPT
    done
    echo "[SANDBOX]   Allowed: AWS S3 us-west-2 CIDRs (${#S3_CIDRS[@]} ranges)"

    # Also resolve the domain at runtime as a fallback for any new prefixes
    local ov_ips
    ov_ips=$(getent ahosts "omniverse-content-production.s3-us-west-2.amazonaws.com" 2>/dev/null \
        | awk '{print $1}' | sort -u) || true
    if [ -n "$ov_ips" ]; then
        for ip in $ov_ips; do
            iptables -A OUTPUT -d "$ip" -p tcp --dport 443 -j ACCEPT 2>/dev/null || true
        done
        echo "[SANDBOX]   Allowed: omniverse CDN runtime IPs ($(echo $ov_ips | wc -w) addrs)"
    fi

    # ── 6. ALLOW: PyPI + Pythonhosted (pip install) ──
    # pypi.org uses Fastly CDN: 151.101.0.0/16, 199.232.0.0/16
    # files.pythonhosted.org uses Fastly: 151.101.0.0/16, 167.82.0.0/16
    local PIP_CIDRS=(
        "151.101.0.0/16"     # Fastly (pypi.org)
        "199.232.0.0/16"     # Fastly alternate
        "167.82.0.0/16"      # Fastly (pythonhosted.org)
    )
    for cidr in "${PIP_CIDRS[@]}"; do
        iptables -A OUTPUT -d "$cidr" -p tcp --dport 443 -j ACCEPT
    done
    echo "[SANDBOX]   Allowed: PyPI/Fastly CIDRs (${#PIP_CIDRS[@]} ranges)"

    # Runtime DNS resolution for pip domains (fallback)
    for domain in "pypi.org" "files.pythonhosted.org"; do
        local pip_ips
        pip_ips=$(getent ahosts "$domain" 2>/dev/null | awk '{print $1}' | sort -u) || true
        if [ -n "$pip_ips" ]; then
            for ip in $pip_ips; do
                iptables -A OUTPUT -d "$ip" -p tcp --dport 443 -j ACCEPT 2>/dev/null || true
            done
        fi
    done
    echo "[SANDBOX]   Allowed: PyPI runtime IPs"

    # ── 7. DROP everything else ──
    iptables -A OUTPUT -j DROP

    echo "[SANDBOX] =============================================="
    echo "[SANDBOX] Firewall ACTIVE (whitelist mode)"
    echo "[SANDBOX]   ALLOW: S3 us-west-2 (Isaac Sim), PyPI (pip)"
    echo "[SANDBOX]   BLOCK: everything else"
    echo "[SANDBOX] =============================================="
}

if command -v iptables &>/dev/null; then
    setup_firewall
else
    echo "[SANDBOX] WARNING: iptables not available — network unrestricted"
fi

# ── Symlink environment cache ────────────────────────────────
if [ -d /sandbox/envs ] && [ "$(ls -A /sandbox/envs 2>/dev/null)" ]; then
    echo "[SANDBOX] Linking environment cache..."
    mkdir -p /root/.cache
    ln -sf /sandbox/envs /root/.cache/nepher
fi

# ── Delete any stale result files ────────────────────────────
echo "[SANDBOX] Cleaning stale result files..."
find /app -name "evaluation_result.json" -delete 2>/dev/null || true
rm -f /sandbox/output/evaluation_result.json

# ── Copy agent files to writable location ────────────────────
echo "[SANDBOX] Copying agent files to writable location..."
cp -r /sandbox/agent /app/agent

# ── Install agent task module ────────────────────────────────
TASK_MODULE=${TASK_MODULE:-""}

if [ -z "$TASK_MODULE" ]; then
    write_error "no_task_module" "TASK_MODULE not specified"
fi

# Find module source
SOURCE_PATH="/app/agent/source/${TASK_MODULE}"
if [ ! -d "$SOURCE_PATH" ]; then
    SOURCE_PATH=$(find /app/agent/source -mindepth 1 -maxdepth 1 -type d | head -1)
fi

if [ -z "$SOURCE_PATH" ] || [ ! -d "$SOURCE_PATH" ]; then
    write_error "module_not_found" "Task module source not found: ${TASK_MODULE}"
fi

echo "[SANDBOX] Installing task module from: ${SOURCE_PATH}"
${ISAACLAB_PATH}/isaaclab.sh -p -m pip install -e "$SOURCE_PATH" 2>&1 || {
    write_error "install_failed" "Task module installation failed"
}

# ── Run evaluation ───────────────────────────────────────────
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

# Bootstrap: force spawn multiprocessing to avoid fork-related GPU issues
BOOTSTRAP="import multiprocessing, sys; multiprocessing.set_start_method('spawn', force=True); sys.argv = sys.argv[1:]; import runpy; runpy.run_path(sys.argv[0], run_name='__main__')"

timeout "${EVAL_TIMEOUT}" ${ISAACLAB_PATH}/isaaclab.sh -p -c "$BOOTSTRAP" \
    "$EVAL_SCRIPT" \
    --config "$EVAL_CONFIG" \
    --headless 2>&1

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

# ── Collect results ──────────────────────────────────────────
if [ ! -f /sandbox/output/evaluation_result.json ]; then
    if [ -f /app/eval-nav/evaluation_result.json ]; then
        echo "[SANDBOX] Moving result from /app/eval-nav/ to output"
        mv /app/eval-nav/evaluation_result.json /sandbox/output/evaluation_result.json
    fi
fi

if [ ! -f /sandbox/output/evaluation_result.json ]; then
    write_error "no_result" "Evaluation completed but no result file generated"
fi

echo "[SANDBOX] Evaluation complete. Result:"
cat /sandbox/output/evaluation_result.json

echo ""
echo "[SANDBOX] Done."
