# Miner Guide

**Nepher Robotics · Bittensor Subnet 49**

Nepher Robotics (Subnet 49) is a **winner-takes-all robotics tournament** on Bittensor. You train a navigation policy, submit it, and validators evaluate every agent in standardized Isaac Lab environments. Only the **single top-scoring miner** earns emissions per tournament.

Your job as a miner: **train a policy → submit**. Validators handle the rest. (Optionally [self-evaluate](#appendix-self-evaluate-same-pipeline-as-validators) first to preview your score.)

---

## 1. Prerequisites

- **GPU** for training (NVIDIA, 24 GB+ VRAM). Submission itself is CPU-only.
- **Python 3.10+**, **Isaac Sim 5.1**, **Isaac Lab 2.3.0**.
- **Bittensor wallet** registered on Subnet 49.
- **Nepher API key** — get one at https://account.nepher.ai → API Keys.

---

## 2. Wallet & Registration

```bash
pip install bittensor
btcli wallet new_coldkey --wallet.name miner
btcli wallet new_hotkey  --wallet.name miner --wallet.hotkey default
btcli subnet register --wallet.name miner --wallet.hotkey default --netuid 49
```

> ⚠️ Back up your coldkey mnemonic. If you lose it, you lose your funds.

---

## 3. Get the Task Repo

Each tournament has its own **task repo** (linked on the [dashboard](https://tournament.nepher.ai)) — an Isaac Lab external project containing the environment, training/play scripts, and the agent layout.

Clone it and follow its **README** for the exact, up-to-date steps to install the task module and register the environments:

```bash
git clone <task-repo-url>
cd <task-repo>
# then follow the repo README to install the task module + environments
```

Install the **Nepher CLI** (used later to submit) and log in:

```bash
pip install nepher-cli
npcli login --api-key nepher_xxxxxxxx             # or run `npcli login` and paste it
```

---

## 4. Train & Prepare Your Policy

Use the training/play scripts and task IDs documented in the repo README. Train on the **generic training task** — validators benchmark on hidden EnvHub datasets, so a policy that generalizes well beats one overfit to specific scenes:

```bash
${ISAACLAB_PATH}/isaaclab.sh -p scripts/rsl_rl/train.py --task=<train-task> --headless
```

Play/test a checkpoint as shown in the README, then copy your best checkpoint to the required submission location:

```bash
cp /path/to/logs/rsl_rl/.../model_best.pt best_policy/best_policy.pt
```

---

## 5. Submit to the Tournament

Submitting needs `bittensor` for wallet signing (the rest comes with `nepher-cli`):

```bash
pip install bittensor                             # or: pip install "nepher-cli[bittensor]"

npcli tournament check --path ./my-agent          # check structure first

npcli tournament submit \
    --path ./my-agent \
    --wallet-name miner \
    --wallet-hotkey default
```

The CLI uses your stored credentials from `npcli login` (or pass `--api-key`). If several tournaments are active, add `--tournament-id <id>`. On success you'll get an **Agent ID** and confirmation — the CLI validates your structure, zips and signs the archive with your wallet, and uploads it.

> 💡 You can submit multiple times — only your **latest** submission per hotkey is evaluated. Submit early so you have time to iterate.

---

## Appendix: Self-Evaluate (same pipeline as validators)

Optional but recommended — run the exact evaluation validators use to preview your score before submitting.

Install `eval-nav` and the **`nepher` EnvHub package** (required to load EnvHub scenes during evaluation):

```bash
git clone https://github.com/nepher-ai/eval-nav.git ./eval-nav
${ISAACLAB_PATH}/isaaclab.sh -p -m pip install -e ./eval-nav
${ISAACLAB_PATH}/isaaclab.sh -p -m pip install nepher
```

Optionally pre-download the benchmark scenes (validators use these exact environments; otherwise they download on demand):

```bash
npcli envhub download waypoint-benchmark-v1
npcli envhub download waypoint-sample-v1
```

`eval-nav` ships ready-made config YAMLs per task under [`configs/`](https://github.com/nepher-ai/eval-nav/tree/main/configs). Copy the one for your task and customize it (e.g. point `policy_path` at your `best_policy.pt`, adjust seeds/episodes):

```bash
cp ./eval-nav/configs/<your-task>.yaml eval_config.yaml
```

Run it:

```bash
${ISAACLAB_PATH}/isaaclab.sh -p ./eval-nav/scripts/evaluate.py \
    --config eval_config.yaml --headless
```

Your score is written to `evaluation_result.json`.

---

## Resources

- Website: https://nepher.ai · Docs: https://docs.nepher.ai · Dashboard: https://tournament.nepher.ai
- Account/API keys: https://account.nepher.ai · Discord: https://discord.gg/qZUc3vdjVq
- Task repo: linked per-tournament on the dashboard/Discord (always read its README) · [eval-nav](https://github.com/nepher-ai/eval-nav) · [envhub](https://github.com/nepher-ai/envhub)
- [Isaac Lab install guide](https://isaac-sim.github.io/IsaacLab/main/source/setup/installation/index.html)

---

**Good luck, miner!**
