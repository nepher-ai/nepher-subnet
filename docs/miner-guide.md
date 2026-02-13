# Nepher Miner Guide

Train a navigation policy and submit it to the **Nepher Subnet 49** tournament.

---

## 1. Prerequisites

- **Python 3.10+**
- **Bittensor wallet** registered on Subnet 49
- **Nepher API key** from https://tournament-api.nepher.ai (Dashboard → API Settings)
- **NVIDIA RTX 4090** or equivalent (for training only — submission is CPU-only)
- **Isaac Lab 2.3.0 + Isaac Sim 5.1** (for training only)

---

## 2. Wallet Setup

```bash
pip install bittensor

btcli wallet new_coldkey --wallet.name miner
btcli wallet new_hotkey --wallet.name miner --wallet.hotkey default
btcli subnet register --wallet.name miner --wallet.hotkey default --netuid 49
```

> **⚠️ Back up your coldkey mnemonic securely.**

---

## 3. Install

```bash
git clone https://github.com/nepher-ai/nepher-subnet.git && cd nepher-subnet
pip install -e .
```

---

## 4. Agent Structure

```
my-agent/
├── best_policy/
│   └── best_policy.pt          # Trained weights (REQUIRED)
├── scripts/                    # Recommended
│   ├── list_envs.py
│   └── rsl_rl/play.py
└── source/
    └── <task_module>/          # REQUIRED — at least one, with __init__.py
```

Validate without submitting:

```bash
nepher-miner validate --path ./my-agent
```

---

## 5. Submit

```bash
cp config/miner_config.example.yaml config/miner_config.yaml
nano config/miner_config.yaml  # Set your API key and wallet name/hotkey
```

```yaml
tournament:
  api_key: "your_api_key_here"

wallet:
  name: "miner"
  hotkey: "default"
```

Then submit:

```bash
nepher-miner submit --path ./my-agent --config config/miner_config.yaml
```

Or with CLI args directly:

```bash
nepher-miner submit --path ./my-agent --wallet-name miner --api-key YOUR_KEY
```

---

## 6. Self-Evaluation with eval-nav

Test your agent locally before submitting using the same evaluation pipeline validators use.

```bash
# Clone and install eval-nav
git clone https://github.com/nepher-ai/eval-nav.git ./eval-nav
${ISAACLAB_PATH}/isaaclab.sh -p -m pip install -e ./eval-nav

# Install your agent's task module
${ISAACLAB_PATH}/isaaclab.sh -p -m pip install -e ./my-agent/source/<task_module>

# Run evaluation
${ISAACLAB_PATH}/isaaclab.sh -p ./eval-nav/scripts/evaluate.py \
    --config eval_config.yaml \
    --headless
```

Create `eval_config.yaml` pointing to your trained policy:

```yaml
policy_path: "./my-agent/best_policy/best_policy.pt"
```

Results are written to `evaluation_result.json` with your score.

---

## 7. Docker (Optional)

```bash
docker compose build miner
docker compose run miner submit --path /app/agent --config /app/config/miner_config.yaml
```

Place your agent in `./agent/` or set `AGENT_PATH`. No GPU required.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Validation fails | Run `nepher-miner validate --path ./my-agent -v` and check the output |
| Connection error | Verify internet / API URL / firewall |
| Wallet not found | Check `~/.bittensor/wallets/miner/` has `coldkey` + `hotkeys/default` |
| "No active tournament" | Submissions only accepted during contest/submit window — check Discord |
| Rejected (not registered) | `btcli subnet register --wallet.name miner --wallet.hotkey default --netuid 49` |

---

## Need Help?

- **Discord:** https://discord.gg/nepher
- **Docs:** https://docs.nepher.ai
- **Issues:** https://github.com/nepher-ai/nepher-subnet/issues
