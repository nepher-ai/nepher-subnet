# Validator CPU/GPU Split

## Problem

The validator runs on a GPU machine for the entire tournament lifecycle, but only the **evaluation period** requires a GPU (Isaac Sim). All other periods — contest, submit window, review, reward, and idle gaps between tournaments — perform only lightweight chain operations (set-weights, burn) that run fine on a CPU. The GPU sits idle for most of the tournament, wasting money.

## Observation

| Period | Work performed | Hardware needed |
|---|---|---|
| No tournament | Poll API | CPU |
| Contest | Poll API | CPU |
| Submit window | Poll API | CPU |
| **Evaluation** | **Download agents, run Isaac Sim, submit scores** | **GPU** |
| Review | Poll API | CPU |
| Reward | Set-weights to winner every hour | CPU |
| Completed / between rewards | Burn on UID 0 every hour | CPU |

Only one out of seven states needs a GPU.

## Proposal

Introduce a single `--mode` flag on the existing CLI (`cpu` | `gpu`). The flag controls which period handlers are active inside `ValidatorOrchestrator`. No new services, no new repos, no IPC — just a behavioural switch over the same codebase.

### CPU Validator (cheap VPS, runs 24/7)

- Runs the main polling loop as today.
- **Reward period** — calls `WeightSetter.run_reward` (set-weights to winner every hour).
- **All other tournament periods** (contest, submit window, evaluation, review, completed) — burns on UID 0 once per hour, then sleeps.
- **No active tournament** — sleeps and polls, no burn needed.
- Skips setup, evaluation, and anything that touches Isaac Sim.

### GPU Validator (default — backward compatible)

- Runs the same main polling loop.
- **Preserves current full behaviour**: setup, evaluation, reward, burn — exactly as the existing single-machine validator.
- When deployed alongside a CPU validator, the GPU machine can be stopped after evaluation ends. The CPU validator takes over weight/burn duties. If both happen to be running during reward, they set the same weights — harmless redundancy.

### How they coexist

Both validators use the **same wallet and hotkey**. Bittensor allows multiple processes to read the wallet; only one process calls `set_weights` at a time (the CPU validator during reward, no conflict). The GPU validator only submits evaluation scores to the tournament API — it never writes to the chain.

No coordination channel is needed between the two. Each independently polls the tournament API, determines the current period, and acts (or does nothing) based on its mode. The worst-case overlap — both running during reward — is harmless because they set identical weights to the same winner.

## Changes Required

### 1. CLI — add `--mode` argument

Add a `--mode {cpu,gpu}` argument to the `run` subcommand in `validator/__main__.py`. Default to `gpu` for backward compatibility (existing single-machine operators change nothing). The CLI flag overrides the config-file value. Pass the value into `ValidatorOrchestrator`.

### 2. Orchestrator — gate period handlers on mode

In `ValidatorOrchestrator.__init__`, accept and store the mode. In `_handle_period`:

- **`gpu` mode** (default) — all handlers active: setup, evaluation, reward, burn. Identical to current behaviour.
- **`cpu` mode** — enable `REWARD` handler; for all other tournament-active periods, run a new `_hourly_burn` handler; skip `EVALUATION` and setup entirely.

### 3. Hourly-burn helper

Extract a small `_hourly_burn` method that reuses `WeightSetter._set_weights(BURN_UID)` on a one-hour cadence. This avoids duplicating weight-setting logic and keeps `WeightSetter` as the single owner of chain interactions.

### 4. Configuration

Add an optional `mode` field to `ValidatorConfig` (default `gpu`). The CLI flag overrides it. No other config changes.

### 5. Docker / Deployment

- Existing `docker-compose.yaml` validator service remains the GPU validator (unchanged).
- Add a second `validator-cpu` service: same image, no `runtime: nvidia`, no GPU resource reservation, entrypoint passes `--mode cpu`. Drop all Isaac Sim cache volumes.
- Operators who want the split run both services; operators who prefer the current single-machine setup change nothing.

## What stays the same

- `WeightSetter`, `EvaluationOrchestrator`, `AgentEvaluator`, `SetupManager` — untouched internally.
- Tournament API client, config loader, state manager — no changes.
- Miner side — unaffected.

## Operator guide (summary)

| Setup | How to run |
|---|---|
| **Single GPU machine** (current) | `nepher-validator run --config …` (default `--mode gpu`, behaves exactly as before) |
| **Split deployment** | GPU machine: `--mode gpu` · CPU machine: `--mode cpu` (same config, same wallet) |

The CPU VPS can be any $5–10/month machine. The GPU machine only needs to be online from shortly before evaluation starts until evaluation ends.

