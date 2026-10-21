#!/bin/bash
# Nepher Sandbox Entrypoint
#
# Runs untrusted miner agent code in isolation with network filtering.
#
# Order matters — NO miner code runs until step 4:
#   0. As root, install the latest nepher and pull eval-nav (network still open).
#   1. Start domain-filtering proxy (SNI for HTTPS, Host header for HTTP)
#      as the dedicated `sniproxy` user.
#   2. iptables: redirect outbound HTTP/HTTPS → proxy, drop everything else.
#   3. Copy the agent to a location owned by the unprivileged `evaluator` user.
#   4. As `evaluator` (no capabilities except read-only DAC_READ_SEARCH,
#      no_new_privs): pip-install the task module and run the evaluation.
#   5. Kill every `evaluator` process, then root copies the result file and
#      logs into /sandbox/output (which `evaluator` cannot write).
#
# Expected mounts:
#   /sandbox/agent   — extracted agent files (read-only)
#   /sandbox/config  — eval_config.yaml + task_config.yaml (read-only)
#   /sandbox/output  — evaluation_result.json written here (root only)
#   /sandbox/envs    — nepher environment cache (read-only)
#
# Environment variables:
#   TASK_MODULE       — name of the task module to install (e.g., spotwaypointnav)
#   EVAL_TIMEOUT      — evaluation timeout in seconds (default: 3600)
#   SANDBOX_WHITELIST — comma-separated domains reachable via the proxy

set -e

export ISAACLAB_PATH=${ISAACLAB_PATH:-/isaac-lab}
export ISAACSIM_PATH=${ISAACSIM_PATH:-/isaac-sim}

EVAL_USER=evaluator
RUN_DIR=/app/run
USERBASE=/app/agent-userbase
RESULT_FILE="${RUN_DIR}/evaluation_result.json"
OUTPUT_RESULT=/sandbox/output/evaluation_result.json
MAX_LOG_BYTES=$((200 * 1024 * 1024))

# ── Helper: write error result and exit ────────────────────────
# JSON is produced by python so arbitrary text in the summary stays valid.
write_error() {
    local error_code="$1"
    local summary="$2"
    echo "[ERROR] ${summary}"
    ERR_CODE="$error_code" ERR_SUMMARY="$summary" python3 -c '
import json, os
print(json.dumps({"score": 0, "metadata": {"error": os.environ["ERR_CODE"]},
                  "summary": os.environ["ERR_SUMMARY"]}))
' > "$OUTPUT_RESULT"
    exit 1
}

# Fail closed: any firewall command error aborts the evaluation.
fw() {
    "$@" || write_error "firewall_failed" "Firewall rule failed: $*"
}

echo "=============================================="
echo "Nepher Sandbox Container"
echo "=============================================="
echo "Task Module: ${TASK_MODULE:-unknown}"
echo "Timeout:     ${EVAL_TIMEOUT:-3600}s"
echo "=============================================="

rm -f "$OUTPUT_RESULT"

# ── GPU pre-flight ─────────────────────────────────────────────
echo "[SANDBOX] Checking GPU availability..."
if ! nvidia-smi &>/dev/null; then
    write_error "gpu_unavailable" "GPU not accessible in sandbox"
fi
echo "[SANDBOX] GPU OK:"
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader

# ── Runtime install: nepher (EnvHub) and eval-nav ──────────────
# Must run BEFORE firewall setup — PyPI and GitHub are blocked after iptables rules apply.
# Runs as root before any miner code is present in the container.
echo "[SANDBOX] Installing latest nepher (EnvHub)..."
if ! ${ISAACLAB_PATH}/isaaclab.sh -p -m pip install --quiet --no-cache-dir nepher; then
    write_error "nepher_install_failed" "Failed to install nepher (EnvHub) at container start"
fi
echo "[SANDBOX] nepher installed: $(${ISAACLAB_PATH}/isaaclab.sh -p -m pip show nepher | grep ^Version)"

echo "[SANDBOX] Updating eval-nav..."
if git -C /app/eval-nav pull --quiet; then
    ${ISAACLAB_PATH}/isaaclab.sh -p -m pip install --quiet --no-build-isolation -e /app/eval-nav \
        || echo "[SANDBOX] WARN: eval-nav reinstall after pull failed — continuing with image-baked version"
else
    echo "[SANDBOX] WARN: eval-nav git pull failed — continuing with image-baked version"
fi

# ── Network firewall (transparent proxy + iptables) ────────────
echo "[SANDBOX] Setting up network firewall..."
SNI_WHITELIST="${SANDBOX_WHITELIST:-}"

# Start the domain-filtering proxy as a dedicated user (sniproxy).
# Running as a separate UID lets iptables exempt the proxy's own outbound
# connections from being redirected back to itself. The whitelist is passed
# as a single argv entry (never through a shell), and the proxy validates
# every domain itself.
echo "[SANDBOX] Starting proxy (whitelist: ${SNI_WHITELIST})..."
setpriv --reuid=sniproxy --regid=sniproxy --init-groups --no-new-privs \
        --inh-caps=-all --bounding-set=-all \
    python3 /usr/local/bin/sni-proxy.py --https-port 3129 --http-port 3128 \
        --whitelist "$SNI_WHITELIST" &

# Wait until the proxy is actually accepting connections before applying
# rules; otherwise iptables would redirect to a port nobody is listening on.
PROXY_READY=false
for i in $(seq 1 20); do
    if (exec 3<>/dev/tcp/127.0.0.1/3129) 2>/dev/null \
       && (exec 3<>/dev/tcp/127.0.0.1/3128) 2>/dev/null; then
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
fw iptables -t nat -A OUTPUT -m owner --uid-owner sniproxy -j RETURN
fw iptables -t nat -A OUTPUT -p tcp --dport 443 -j REDIRECT --to-port 3129
fw iptables -t nat -A OUTPUT -p tcp --dport 80  -j REDIRECT --to-port 3128

# ── iptables filter: restrict what can reach the network ───────
fw iptables -A OUTPUT -o lo -j ACCEPT
# Allow NAT-redirected packets to reach the proxy
fw iptables -A OUTPUT -d 127.0.0.1/32 -p tcp --dport 3129 -j ACCEPT
fw iptables -A OUTPUT -d 127.0.0.1/32 -p tcp --dport 3128 -j ACCEPT
fw iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
# DNS — only allow queries to the container's configured resolver.
# Open DNS (to any IP) would allow DNS tunneling for data exfiltration.
NAMESERVER=$(awk '/^nameserver/{print $2; exit}' /etc/resolv.conf)
if [ -n "$NAMESERVER" ]; then
    fw iptables -A OUTPUT -d "$NAMESERVER" -p udp --dport 53 -j ACCEPT
    fw iptables -A OUTPUT -d "$NAMESERVER" -p tcp --dport 53 -j ACCEPT
else
    # Fallback: allow DNS to Docker's default resolver only
    fw iptables -A OUTPUT -d 127.0.0.11 -p udp --dport 53 -j ACCEPT
    fw iptables -A OUTPUT -d 127.0.0.11 -p tcp --dport 53 -j ACCEPT
fi
# Block all other DNS (prevents DNS tunneling to attacker-controlled servers)
fw iptables -A OUTPUT -p udp --dport 53 -j DROP
fw iptables -A OUTPUT -p tcp --dport 53 -j DROP
# Block cloud metadata endpoint (prevents IAM credential theft)
fw iptables -A OUTPUT -d 169.254.0.0/16 -j DROP
# Block private networks (prevents lateral movement)
fw iptables -A OUTPUT -d 10.0.0.0/8 -j DROP
fw iptables -A OUTPUT -d 172.16.0.0/12 -j DROP
fw iptables -A OUTPUT -d 192.168.0.0/16 -j DROP
# Allow proxy user to connect to whitelisted servers
fw iptables -A OUTPUT -m owner --uid-owner sniproxy -j ACCEPT
# Drop ALL remaining traffic (TCP, UDP, ICMP — everything)
fw iptables -A OUTPUT -j DROP

# ── IPv6: block all outbound (IPv4 rules don't cover IPv6) ────
if command -v ip6tables >/dev/null 2>&1; then
    fw ip6tables -A OUTPUT -o lo -j ACCEPT
    fw ip6tables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
    fw ip6tables -A OUTPUT -j DROP
fi

echo "[SANDBOX] Firewall active — only whitelisted domains reachable"

# ── Link environment cache ─────────────────────────────────────
# Cache symlinks created by the validator point at /root/.cache/nepher/...,
# so keep that path and let the evaluator traverse (not list) /root.
if [ -d /sandbox/envs ] && [ "$(ls -A /sandbox/envs 2>/dev/null)" ]; then
    echo "[SANDBOX] Linking environment cache..."
    mkdir -p /root/.cache
    ln -sfn /sandbox/envs /root/.cache/nepher
    chmod 711 /root /root/.cache
fi

# ── Stage agent for the unprivileged evaluator ─────────────────
TASK_MODULE=${TASK_MODULE:-""}
if [ -z "$TASK_MODULE" ]; then
    write_error "no_task_module" "TASK_MODULE not specified"
fi
case "$TASK_MODULE" in
    *[!A-Za-z0-9_.-]*) write_error "no_task_module" "Invalid TASK_MODULE name" ;;
esac

EVAL_SCRIPT="/app/eval-nav/scripts/evaluate.py"
EVAL_CONFIG="/sandbox/config/eval_config.yaml"
EVAL_TIMEOUT=${EVAL_TIMEOUT:-3600}
case "$EVAL_TIMEOUT" in
    ''|*[!0-9]*) EVAL_TIMEOUT=3600 ;;
esac

if [ ! -f "$EVAL_SCRIPT" ]; then
    write_error "eval_script_missing" "Evaluation script not found: ${EVAL_SCRIPT}"
fi
if [ ! -f "$EVAL_CONFIG" ]; then
    write_error "eval_config_missing" "Evaluation config not found: ${EVAL_CONFIG}"
fi

echo "[SANDBOX] Copying agent to /app/agent..."
rm -rf /app/agent "$RUN_DIR" "$USERBASE" /app/logs
cp -R --no-preserve=ownership /sandbox/agent /app/agent
mkdir -p "$RUN_DIR" "$USERBASE" /app/logs
chown -R "${EVAL_USER}:${EVAL_USER}" /app/agent "$RUN_DIR" "$USERBASE" /app/logs

SOURCE_PATH="/app/agent/source/${TASK_MODULE}"
if [ ! -d "$SOURCE_PATH" ]; then
    SOURCE_PATH=$(find /app/agent/source -mindepth 1 -maxdepth 1 -type d 2>/dev/null | head -1)
fi
if [ -z "$SOURCE_PATH" ] || [ ! -d "$SOURCE_PATH" ]; then
    write_error "module_not_found" "Task module source not found: ${TASK_MODULE}"
fi

# Python multiprocessing bootstrap: force 'spawn' start method (required by
# Isaac Sim 6.x / Python 3.12), register the evaluator's user site (where the
# task module is installed, incl. its editable .pth) and re-route argv so that
# the eval script runs as __main__. Isaac Lab 3.0 rejects --headless;
# --viz none disables visualizers.
BOOTSTRAP="import multiprocessing, site, sys; multiprocessing.set_start_method('spawn', force=True); site.addsitedir(site.getusersitepackages()); sys.argv = sys.argv[1:]; import runpy; runpy.run_path(sys.argv[0], run_name='__main__')"

USER_SITE=$(PYTHONUSERBASE="$USERBASE" ${ISAACLAB_PATH}/isaaclab.sh -p -c \
    "import site; print(site.getusersitepackages())" 2>/dev/null | tail -1)

echo "[SANDBOX] Installing task module from: ${SOURCE_PATH}"
echo "[SANDBOX] Dropping to user '${EVAL_USER}' — no capabilities beyond read-only DAC_READ_SEARCH"

# Disable set -e around miner code so a non-zero exit (e.g. Isaac Sim crashing
# on shutdown AFTER a successful run, which makes `timeout` return 124) does
# NOT abort the entrypoint before result collection below.
set +e

# Everything below runs as `evaluator`:
#   - uid/gid != root, so the root-owned scorer (/app/eval-nav), Isaac Lab and
#     /sandbox/output cannot be modified;
#   - only DAC_READ_SEARCH (read-only) is kept — the same access the root
#     eval had for Isaac Sim's files; NET_ADMIN/SETUID/SETGID/SETPCAP are gone
#     from every set including the bounding set;
#   - no_new_privs blocks setuid binaries.
run_as_evaluator() {
    setpriv --reuid="$EVAL_USER" --regid="$EVAL_USER" --init-groups --no-new-privs \
            --inh-caps=-all,+dac_read_search --ambient-caps=-all,+dac_read_search \
            --bounding-set=-all,+dac_read_search \
        env PYTHONUSERBASE="$USERBASE" PYTHONPATH="${USER_SITE}${PYTHONPATH:+:$PYTHONPATH}" \
        "$@"
}

(
    cd "$RUN_DIR" || exit 1
    run_as_evaluator ${ISAACLAB_PATH}/isaaclab.sh -p -m pip install --user --no-cache-dir \
        --no-build-isolation --no-deps -e "$SOURCE_PATH" 2>&1
)
INSTALL_EXIT=$?
pkill -KILL -u "$EVAL_USER" 2>/dev/null
if [ $INSTALL_EXIT -ne 0 ]; then
    write_error "install_failed" "Task module installation failed"
fi

echo "[SANDBOX] Running evaluation (timeout: ${EVAL_TIMEOUT}s)..."
(
    cd "$RUN_DIR" || exit 1
    run_as_evaluator timeout -k 30 "${EVAL_TIMEOUT}" \
        ${ISAACLAB_PATH}/isaaclab.sh -p -c "${BOOTSTRAP}" \
        "${EVAL_SCRIPT}" \
        --config "${EVAL_CONFIG}" \
        --result-path "${RESULT_FILE}" \
        --viz none 2>&1
)
EVAL_EXIT=$?

# No evaluator process may survive into result collection (prevents
# swapping the result file for a symlink after the checks below).
pkill -KILL -u "$EVAL_USER" 2>/dev/null
sleep 1
pkill -KILL -u "$EVAL_USER" 2>/dev/null

set -e

# ── Collect results (root only) ────────────────────────────────
# Only the harness result path is accepted; no searching across /app.
if [ -f "$RESULT_FILE" ] && [ ! -L "$RESULT_FILE" ]; then
    cp --no-dereference "$RESULT_FILE" "$OUTPUT_RESULT"
fi

# Only synthesize an error result if the eval failed AND produced nothing.
if [ $EVAL_EXIT -ne 0 ] && [ ! -f "$OUTPUT_RESULT" ]; then
    echo "[SANDBOX] Evaluation exited with code: ${EVAL_EXIT}"
    if [ $EVAL_EXIT -eq 124 ] || [ $EVAL_EXIT -eq 137 ]; then
        MSG="Evaluation timed out after ${EVAL_TIMEOUT}s"
    else
        MSG="Evaluation script failed with exit code ${EVAL_EXIT}"
    fi
    ERR_EXIT="$EVAL_EXIT" ERR_SUMMARY="$MSG" python3 -c '
import json, os
print(json.dumps({"score": 0, "metadata": {"error": "eval_failed",
                  "exit_code": int(os.environ["ERR_EXIT"])},
                  "summary": os.environ["ERR_SUMMARY"]}))
' > "$OUTPUT_RESULT"
fi

# Copy eval logs to output for the validator (symlinks are copied as links
# and ignored by the validator; oversized log trees are skipped).
LOG_DIR=$(find "$RUN_DIR/logs" /app/logs -mindepth 2 -maxdepth 2 -type d -name "eval_run_*" 2>/dev/null | sort | tail -1)
if [ -n "$LOG_DIR" ] && [ ! -L "$LOG_DIR" ]; then
    LOG_BYTES=$(du -sb "$LOG_DIR" 2>/dev/null | cut -f1)
    if [ -n "$LOG_BYTES" ] && [ "$LOG_BYTES" -le "$MAX_LOG_BYTES" ]; then
        echo "[SANDBOX] Copying eval logs from: ${LOG_DIR}"
        cp -RP --no-preserve=ownership "$LOG_DIR" /sandbox/output/eval_logs
    else
        echo "[SANDBOX] WARN: eval logs too large (${LOG_BYTES} bytes) — not copied"
    fi
fi

if [ ! -f "$OUTPUT_RESULT" ]; then
    write_error "no_result" "Evaluation completed but no result file generated"
fi

echo "[SANDBOX] Evaluation complete. Result:"
head -c 4096 "$OUTPUT_RESULT"
echo ""
echo "[SANDBOX] Done."
