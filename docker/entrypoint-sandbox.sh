#!/bin/bash
# Nepher Sandbox Entrypoint
#
# Runs untrusted miner agent code in isolation with network filtering.
#
# Network security (transparent proxy + iptables):
#   1. Start domain-filtering proxy (SNI for HTTPS, Host header for HTTP)
#   2. iptables NAT redirects ALL outbound HTTP/HTTPS → proxy
#   3. iptables blocks ALL direct outbound (only proxy can reach internet)
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

# ── GPU driver library alignment ──────────────────────────────
# The Isaac Sim base image bundles NVIDIA driver userspace libraries
# (Vulkan, GL, RTX) INSIDE its own directory tree (/isaac-sim/...).
# The NVIDIA Container Toolkit may replace the copies under /usr/lib/
# with the host version, but Isaac Sim loads from its OWN directories
# via LD_LIBRARY_PATH — so the old bundled version always wins and
# Omniverse rejects it as unsupported for RTX.
#
# Fix strategy:
#   1. Find host driver libraries (mounted by the toolkit).
#   2. Search the ENTIRE container for stale-version NVIDIA libs
#      (especially inside /isaac-sim/) and replace them in-place
#      with symlinks to the host version.
#   3. Create an override directory and patch Isaac Sim's env setup.
#   4. Update all Vulkan ICD manifests.
#   5. Disable the RTX driver version check in the Kit config as a
#      safety net (the actual kernel driver IS the host version).
_align_gpu_driver_libs() {
    local host_ver
    host_ver=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null \
               | head -1 | tr -d '[:space:]')
    [ -z "$host_ver" ] && return 0

    echo "[SANDBOX] Host GPU driver version: ${host_ver}"

    # Find host-version libraries (toolkit mounts them in system lib dirs).
    local host_lib_dir=""
    for d in /usr/lib/x86_64-linux-gnu /usr/lib64 /usr/lib; do
        if [ -f "${d}/libnvidia-glcore.so.${host_ver}" ]; then
            host_lib_dir="$d"
            break
        fi
    done
    if [ -z "$host_lib_dir" ]; then
        local _found
        _found=$(find /usr -maxdepth 5 -name "libnvidia-glcore.so.${host_ver}" 2>/dev/null | head -1)
        [ -n "$_found" ] && host_lib_dir=$(dirname "$_found")
    fi

    if [ -z "$host_lib_dir" ]; then
        echo "[SANDBOX] Warning: host driver ${host_ver} userspace libs not found"
        echo "[SANDBOX] Toolkit may not have mounted graphics libraries"
        _disable_rtx_driver_check
        return 0
    fi

    echo "[SANDBOX] Host driver libs at: ${host_lib_dir}"

    # ── Step 1: Find and replace ALL stale driver libs filesystem-wide ──
    # Isaac Sim bundles its own copies under /isaac-sim/kit/libs/ and
    # similar paths. These are what Omniverse actually loads.
    local replaced=0
    while IFS= read -r stale_lib; do
        [ -f "$stale_lib" ] || continue
        local name ver base host_equiv
        name=$(basename "$stale_lib")

        # Extract version suffix (e.g., 535.32.01 from libnvidia-glcore.so.535.32.01)
        # grep exits 1 when there is no match — with set -e that would abort the whole
        # entrypoint before any evaluation_result.json is written; never fail here.
        ver=$(echo "$name" | grep -oP '\.so\.\K[0-9]+\.[0-9.]+$' || true)
        [ -z "$ver" ] && continue
        [ "$ver" = "$host_ver" ] && continue

        # Find the matching host library
        base=$(echo "$name" | sed "s/\.${ver}$//")
        host_equiv="${host_lib_dir}/${base}.${host_ver}"
        [ -f "$host_equiv" ] || continue

        # Replace the stale library with a symlink to the host version
        ln -sf "$host_equiv" "$stale_lib" 2>/dev/null && replaced=$((replaced + 1))
    # grep exits 1 when nothing passes the filter — must not trip set -e
    done < <(find /isaac-sim /usr/lib /opt 2>/dev/null \
             -name "libnvidia-*.so.*" -o \
             -name "libcuda.so.*" -o \
             -name "libGLX_nvidia.so.*" -o \
             -name "libvdpau_nvidia.so.*" \
             | grep -v "\.${host_ver}$" || true)

    echo "[SANDBOX] Replaced ${replaced} stale driver libs → ${host_ver}"

    # ── Step 2: Override directory + LD_LIBRARY_PATH ──
    local override_dir="/app/nvidia-driver-override"
    mkdir -p "$override_dir"

    for host_lib in "${host_lib_dir}"/lib*nvidia*.so."${host_ver}" \
                    "${host_lib_dir}"/libcuda.so."${host_ver}" \
                    "${host_lib_dir}"/libGLX_nvidia.so.0."${host_ver}"; do
        [ -f "$host_lib" ] || continue
        local name base
        name=$(basename "$host_lib")
        base="${name%.${host_ver}}"
        ln -sf "$host_lib" "${override_dir}/${name}" 2>/dev/null || true
        ln -sf "$host_lib" "${override_dir}/${base}" 2>/dev/null || true
        case "$base" in
            *.so.[0-9]*) ;;
            *) ln -sf "$host_lib" "${override_dir}/${base}.1" 2>/dev/null || true ;;
        esac
    done

    export LD_LIBRARY_PATH="${override_dir}:${host_lib_dir}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

    # ── Step 3: Patch Isaac Sim's env setup so override survives subprocesses ──
    # Image layers may mount /isaac-sim read-only — append must never abort (set -e).
    local script
    for script in "${ISAACSIM_PATH}/setup_python_env.sh" \
                  "${ISAACSIM_PATH}/setup_conda_env.sh"; do
        [ -f "$script" ] || continue
        if ! grep -q "nvidia-driver-override" "$script" 2>/dev/null; then
            if printf '\n# [nepher-sandbox] Host GPU driver override\nexport LD_LIBRARY_PATH="%s:%s:${LD_LIBRARY_PATH}"\n' \
                "$override_dir" "$host_lib_dir" >> "$script" 2>/dev/null; then
                echo "[SANDBOX] Patched $(basename "$script")"
            else
                echo "[SANDBOX] Warning: $(basename "$script") is not writable — relying on LD_LIBRARY_PATH in this shell"
            fi
        fi
    done

    # ── Step 4: Vulkan ICD manifests (system-wide + inside Isaac Sim) ──
    while IFS= read -r icd; do
        sed -i "s/libnvidia-vulkan-producer\.so\.[0-9.]*/libnvidia-vulkan-producer.so.${host_ver}/g" \
            "$icd" 2>/dev/null || true
    done < <(find / -maxdepth 7 -name "nvidia_icd*.json" 2>/dev/null)

    ldconfig 2>/dev/null || true

    # ── Step 5: Disable RTX driver version check (safety net) ──
    _disable_rtx_driver_check

    echo "[SANDBOX] GPU driver alignment complete → ${host_ver}"
}

_disable_rtx_driver_check() {
    # Omniverse Kit's gpu.foundation.plugin has a conservative driver
    # version check. When the kernel module is a valid host driver but
    # userspace libs report a stale version (or when we've just replaced
    # them), the check can still fire before the fix fully takes effect.
    # Disabling it is safe because the actual kernel driver IS supported.
    local kit_files=(
        "${ISAACLAB_PATH}/apps/isaaclab.python.headless.rendering.kit"
        "${ISAACLAB_PATH}/apps/isaaclab.python.headless.kit"
    )
    for kit_file in "${kit_files[@]}"; do
        [ -f "$kit_file" ] || continue
        if ! grep -q "verifyDriverVersion" "$kit_file" 2>/dev/null; then
            if printf '\n[settings]\nrtx.verifyDriverVersion.enabled = false\n' >> "$kit_file" 2>/dev/null; then
                echo "[SANDBOX] Disabled RTX driver version check in $(basename "$kit_file")"
            else
                echo "[SANDBOX] Warning: could not patch $(basename "$kit_file") (read-only)"
            fi
        fi
    done
}

_align_gpu_driver_libs

# ── Network firewall (transparent proxy + iptables) ────────────
echo "[SANDBOX] Setting up network firewall..."

# Whitelist: only these domains are reachable from the sandbox.
# Fetched from the tournament API and passed via SANDBOX_WHITELIST env var.
# Fallback to built-in defaults if not set (e.g. during manual testing).
DEFAULT_WHITELIST=""
SNI_WHITELIST="${SANDBOX_WHITELIST:-$DEFAULT_WHITELIST}"

# Start the domain-filtering proxy as a dedicated user (sniproxy).
# Running as a separate UID lets iptables exempt the proxy's own outbound
# connections from being redirected back to itself.
echo "[SANDBOX] Starting proxy (whitelist: ${SNI_WHITELIST})..."
su -s /bin/sh sniproxy -c \
    "python3 /usr/local/bin/sni-proxy.py --https-port 3129 --http-port 3128 \
     --whitelist '${SNI_WHITELIST}'" &

# Wait for proxy to bind its ports before applying firewall rules.
# Without this, there's a window where iptables redirects to a port nobody is listening on.
PROXY_READY=false
for i in $(seq 1 10); do
    if pgrep -u sniproxy python3 >/dev/null 2>&1; then
        PROXY_READY=true
        break
    fi
    sleep 0.5
done

if [ "$PROXY_READY" = true ]; then
    echo "[SANDBOX] Proxy started (HTTPS=3129, HTTP=3128)"
else
    write_error "proxy_failed" "Network proxy failed to start — aborting for security"
fi

# ── iptables NAT: redirect outbound HTTP/HTTPS to proxy ───────
# Skip redirect for the proxy's own outbound connections (uid sniproxy)
iptables -t nat -A OUTPUT -m owner --uid-owner sniproxy -j RETURN
iptables -t nat -A OUTPUT -p tcp --dport 443 -j REDIRECT --to-port 3129
iptables -t nat -A OUTPUT -p tcp --dport 80  -j REDIRECT --to-port 3128

# ── iptables filter: restrict what can reach the network ───────
iptables -A OUTPUT -o lo -j ACCEPT
# Allow NAT-redirected packets to reach the proxy
iptables -A OUTPUT -d 127.0.0.1/32 -p tcp --dport 3129 -j ACCEPT
iptables -A OUTPUT -d 127.0.0.1/32 -p tcp --dport 3128 -j ACCEPT
iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
# DNS — only allow queries to the container's configured resolver.
# Open DNS (to any IP) would allow DNS tunneling for data exfiltration.
NAMESERVER=$(awk '/^nameserver/{print $2; exit}' /etc/resolv.conf)
if [ -n "$NAMESERVER" ]; then
    iptables -A OUTPUT -d "$NAMESERVER" -p udp --dport 53 -j ACCEPT
    iptables -A OUTPUT -d "$NAMESERVER" -p tcp --dport 53 -j ACCEPT
else
    # Fallback: allow DNS to Docker's default resolver only
    iptables -A OUTPUT -d 127.0.0.11 -p udp --dport 53 -j ACCEPT
    iptables -A OUTPUT -d 127.0.0.11 -p tcp --dport 53 -j ACCEPT
fi
# Block all other DNS (prevents DNS tunneling to attacker-controlled servers)
iptables -A OUTPUT -p udp --dport 53 -j DROP
iptables -A OUTPUT -p tcp --dport 53 -j DROP
# Block cloud metadata endpoint (prevents IAM credential theft)
iptables -A OUTPUT -d 169.254.0.0/16 -j DROP
# Block private networks (prevents lateral movement)
iptables -A OUTPUT -d 10.0.0.0/8 -j DROP
iptables -A OUTPUT -d 172.16.0.0/12 -j DROP
iptables -A OUTPUT -d 192.168.0.0/16 -j DROP
# Allow proxy user to connect to whitelisted servers
iptables -A OUTPUT -m owner --uid-owner sniproxy -j ACCEPT
# Drop ALL remaining traffic (TCP, UDP, ICMP — everything)
iptables -A OUTPUT -j DROP

# ── IPv6: block all outbound (IPv4 rules don't cover IPv6) ────
if command -v ip6tables >/dev/null 2>&1; then
    ip6tables -A OUTPUT -o lo -j ACCEPT
    ip6tables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
    ip6tables -A OUTPUT -j DROP
fi

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

# ── Run evaluation ─────────────────────────────────────────────
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
echo "[SANDBOX] Dropping capabilities — firewall is now immutable"

# Python multiprocessing bootstrap: force 'spawn' start method (required by
# Isaac Sim) and re-route argv so that the eval script runs as __main__.
BOOTSTRAP="import multiprocessing, sys; multiprocessing.set_start_method('spawn', force=True); sys.argv = sys.argv[1:]; import runpy; runpy.run_path(sys.argv[0], run_name='__main__')"

# Disable set -e so a non-zero exit from the evaluation does not skip
# the fallback result-file write and log collection below.
set +e

# Drop capabilities so miner code cannot:
#   - modify iptables rules  (NET_ADMIN)
#   - re-grant capabilities  (SETPCAP)
#   - switch uid/gid         (SETUID/SETGID)
capsh --drop=cap_net_admin,cap_setpcap,cap_setuid,cap_setgid -- -c "
    timeout ${EVAL_TIMEOUT} ${ISAACLAB_PATH}/isaaclab.sh -p -c \"${BOOTSTRAP}\" \
        \"${EVAL_SCRIPT}\" \
        --config \"${EVAL_CONFIG}\" \
        --headless 2>&1
"

EVAL_EXIT=$?
set -e

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
