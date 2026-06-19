# Sandbox Evaluation

**Nepher Robotics · Bittensor Subnet 49**

Validators never run Isaac Sim directly. For each agent, the validator spawns an ephemeral **`nepher-sandbox`** Docker container (~20 GB, Isaac Sim 5.1 + Isaac Lab 2.3.2), runs evaluation inside it, reads the score, then discards the container. Untrusted miner code is fully isolated — it cannot reach the validator, the host, or any other agent.

---

## How It Works

1. **Download** — validator downloads the miner's agent ZIP from the Tournament API.
2. **Configure** — writes `eval_config.yaml` (task, scenes, seeds, policy path) to a per-eval workspace.
3. **Spawn sandbox** — `docker run nepher-sandbox` with GPU, resource limits, and a network whitelist.
4. **Inside the container:**
   - GPU pre-flight (`nvidia-smi`) — failure → `score: 0`, exit.
   - Install `nepher` (EnvHub) + update `eval-nav` (before firewall).
   - Activate network firewall — all outbound blocked except whitelisted domains.
   - `pip install` miner's task module from `agent/source/<TASK_MODULE>`.
   - Run: `isaaclab.sh -p evaluate.py --config eval_config.yaml --headless`
   - Write `evaluation_result.json` to `/sandbox/output/`.
5. **Submit** — validator reads `score` from the result file and posts it to the Tournament API.

---

## Security

Miner code runs with **no path to the validator** — no wallet, no Docker socket, no writable host mounts, dropped capabilities, hard resource limits, and a network firewall that blocks everything except whitelisted domains. Each container is discarded after use. A miner cannot influence their own score, reach other agents, or persist anything on the host.

| Constraint | Detail |
|------------|--------|
| No wallet / no Docker socket mounted | Miner code cannot reach validator credentials or spawn containers |
| `--cap-drop ALL` + `capsh` before eval | Capabilities are minimal and dropped further before miner code runs |
| `--memory 32g` · `--pids-limit 4096` | Hard resource ceiling per evaluation |
| SNI proxy firewall | Whitelisted domains only; all other outbound traffic blocked |
| `--rm` | Container is discarded after each evaluation |

---

## Failure Modes

A `score: 0` is submitted with one of these error types:

`gpu_unavailable` · `timeout` · `import_error` · `runtime_error` · `network_violation` · `sandbox_escape`

Evaluation timeout is **1800 s**; the hard container kill is `timeout + 120 s`.

---

## Score Result

`eval-nav` writes `evaluation_result.json`:

```json
{ "score": 0.87, "summary": "...", "metadata": { ... } }
```

Scores from all validators are aggregated stake-weighted by the backend. Only the **latest** agent per miner hotkey is scored. See [Incentive Mechanism](./incentive_mechanism.md) for weight distribution.
