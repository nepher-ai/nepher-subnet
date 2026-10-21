# Sandbox Evaluation

**Nepher Robotics · Bittensor Subnet 49**

Validators never run Isaac Sim directly. For each agent, the validator spawns an ephemeral **`nepher-sandbox`** Docker container (~20 GB, Isaac Sim 6.1 + Isaac Lab 3.0.0), runs evaluation inside it, reads the score, then discards the container. Untrusted miner code is fully isolated — it cannot reach the validator, the host, or any other agent.

---

## How It Works

1. **Download** — validator downloads the miner's agent ZIP from the Tournament API.
2. **Configure** — writes `eval_config.yaml` (task, scenes, seeds, policy path) to a per-eval workspace.
3. **Spawn sandbox** — `docker run nepher-sandbox:isaacsim6.1-lab3.0` with GPU, resource limits, and a network whitelist.
4. **Inside the container:**
   - GPU pre-flight (`nvidia-smi`).
   - Install `nepher` (EnvHub) + update `eval-nav` (as root, before firewall and before any miner code).
   - Activate network firewall — all outbound blocked except whitelisted domains.
   - Switch to the unprivileged `evaluator` user (no capabilities except read-only `DAC_READ_SEARCH`, `no_new_privs`).
   - As `evaluator`: `pip install` the miner's task module from `agent/source/<TASK_MODULE>`, then run `isaaclab.sh -p evaluate.py --config eval_config.yaml --result-path /app/run/evaluation_result.json --viz none`.
   - Kill all `evaluator` processes; root copies the result and logs into `/sandbox/output/` (not writable by `evaluator`).
5. **Submit** — validator validates the result (numeric score, size limit, no symlinks; `log_dir` is always set validator-side) and posts it to the Tournament API.

---

## Security

Miner code runs with **no path to the validator** — no wallet, no Docker socket, no writable host mounts, dropped capabilities, hard resource limits, and a network firewall that blocks everything except whitelisted domains. Each container is discarded after use. Miner code never runs as root and cannot modify the scorer, the firewall, or the output volume.

Miner policy code still runs in the same Python process as the simulator and scorer, so in-process tampering with the score is not prevented by the sandbox; cross-validator score aggregation is the defence there.

| Constraint | Detail |
|------------|--------|
| No wallet / no Docker socket mounted | Miner code cannot reach validator credentials or spawn containers |
| `--cap-drop ALL` + `setpriv` to `evaluator` | Root-only setup (firewall, proxy) finishes before any miner code; miner code runs as a non-root user with only read-only `DAC_READ_SEARCH` |
| `--memory 32g` · `--pids-limit 4096` | Hard resource ceiling per evaluation |
| SNI proxy firewall | Whitelisted domains only; the proxy dials the whitelisted hostname itself, never the client-chosen IP |
| `--rm` | Container is discarded after each evaluation |

---

## Failure Modes

A `score: 0` is submitted when the agent is at fault, with one of these error types:

`timeout` · `import_error` · `runtime_error` · `network_violation` · `sandbox_escape`

Validator-side failures (`gpu_unavailable`, `nepher_install_failed`, `proxy_failed`, `firewall_failed`, Docker or API errors, missing tournament config) are **never** scored: the agent stays pending and is retried up to `retry.evaluation_max_attempts` times per validator run.

Evaluation timeout is **1800 s**; the hard container kill is `timeout + 120 s`.

---

## Score Result

`eval-nav` writes `evaluation_result.json`:

```json
{ "score": 0.87, "summary": "...", "metadata": { ... } }
```

Scores from all validators are aggregated stake-weighted by the backend. Only the **latest** agent per miner hotkey is scored. See [Incentive Mechanism](./incentive_mechanism.md) for weight distribution.
