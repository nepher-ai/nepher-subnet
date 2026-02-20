# Nepher Validator Guide

Set up and run a **Nepher Subnet 49 validator**.

The validator supports two deployment modes:

| Mode | What it does | Hardware |
|---|---|---|
| **GPU** (default) | Full lifecycle — evaluation, set-weights, burn | GPU machine (A100+ recommended) |
| **CPU** | Set-weights & hourly burn only — no evaluation | Any cheap CPU VPS ($5–10/month) |

A single GPU machine with the default mode handles everything. For cost savings you can split the workload across two machines — see [Section 8](#8-cpugpu-split-deployment).

---

## 1. Requirements

### GPU Validator (default)

| Spec | Minimum | Recommended |
|---|---|---|
| **GPU** | RTX A6000 (24 GB VRAM) | A100 (40 GB+) |
| **RAM** | 32 GB | 64 GB+ |
| **Disk** | 100 GB SSD | 200 GB+ NVMe |
| **OS** | Ubuntu 22.04 LTS | Ubuntu 22.04 LTS |
| **NVIDIA Driver / CUDA** | 535+ / 12.1+ | Latest stable / 12.1+ |

**Software:** Isaac Sim 5.1, Isaac Lab 2.3.0, Python 3.10+, Docker + Compose, Git

### CPU Validator (optional split)

| Spec | Minimum |
|---|---|
| **CPU** | 2 vCPU |
| **RAM** | 4 GB |
| **Disk** | 20 GB |
| **OS** | Ubuntu 22.04 LTS |

**Software:** Python 3.10+, Docker + Compose (or native install), Git. No GPU, no Isaac Sim required.

> Most GPU cloud providers ship drivers and Docker pre-installed — skip to [Step 3](#3-bittensor-wallet) if so.

---

## 2. Server Setup (if needed)

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y git curl wget build-essential software-properties-common
nvidia-smi  # If this fails, install drivers below
```

<details><summary><b>Install NVIDIA drivers</b></summary>

```bash
sudo apt install -y nvidia-driver-535 && sudo reboot
```
</details>

<details><summary><b>Install Docker + NVIDIA Container Toolkit</b></summary>

```bash
# Docker
curl -fsSL https://get.docker.com -o get-docker.sh && sudo sh get-docker.sh
sudo usermod -aG docker $USER && newgrp docker

# NVIDIA Container Toolkit
distribution=$(. /etc/os-release;echo $ID$VERSION_ID)
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/$distribution/libnvidia-container.list | \
    sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
    sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt update && sudo apt install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker

# Verify
docker run --rm --gpus all nvidia/cuda:12.1.0-base-ubuntu22.04 nvidia-smi

# Compose plugin
sudo apt install -y docker-compose-plugin
```
</details>

---

## 3. Bittensor Wallet

> **Prerequisite:** Install the Bittensor CLI (`btcli`) if you haven't already — see the [Bittensor docs](https://docs.bittensor.com/).

Register and stake on Subnet 49:

```bash
btcli subnet register --wallet.name validator --wallet.hotkey default --netuid 49
btcli stake add       --wallet.name validator --wallet.hotkey default --amount <AMOUNT>
```

Wallet files: `~/.bittensor/wallets/validator/`

---

## 4. Get Your Nepher API Key

Sign in at **https://account.nepher.ai** → **API Keys** → copy your key.

---

## 5. Option A — Docker (Recommended)

```bash
git clone https://github.com/nepher-ai/nepher-subnet.git && cd nepher-subnet

# Configure
cp config/docker.env.example .env
cp config/validator_config.example.yaml config/validator_config.yaml
```

Edit **`.env`** (used by Docker Compose to inject environment variables):

```bash
NEPHER_API_KEY=nepher_your_actual_api_key_here
```

Edit **`config/validator_config.yaml`** (used by the validator process at runtime):

```yaml
tournament:
  api_key: "nepher_your_actual_api_key_here"
wallet:
  name: "validator"
  hotkey: "default"
```

> **Why both?** `.env` passes the key into the container environment; `validator_config.yaml` is read by the validator itself. Keep them in sync. Shared settings live in `config/common_config.yaml` (ships with repo) and are merged automatically.

```bash
# Build & run (GPU validator — default mode)
docker compose build validator          # First build: 30–60 min (Isaac Sim ~20 GB)
docker compose up -d validator
docker compose logs -f validator

# Manage
docker compose down                     # Stop
docker compose restart validator        # Restart
docker compose up -d --build validator  # Rebuild after updates
docker compose exec validator bash      # Shell into container
```

> **CPU-only mode (Docker):** `docker compose up -d validator-cpu` — see [Section 8](#8-cpugpu-split-deployment).

---

## 6. Option B — Native Install

### Isaac Sim 5.1 & Isaac Lab 2.3.0

Follow the [NVIDIA Isaac Sim install guide](https://docs.omniverse.nvidia.com/isaacsim/latest/installation/install_workstation.html), then:

```bash
export ISAACSIM_PATH=/path/to/isaac-sim
export ISAACLAB_PATH=/path/to/isaac-lab

git clone https://github.com/isaac-sim/IsaacLab.git $ISAACLAB_PATH
cd $ISAACLAB_PATH && git checkout v2.3.0 && ./isaaclab.sh --install
```

### Nepher Subnet

```bash
cd ~ && git clone https://github.com/nepher-ai/nepher-subnet.git && cd nepher-subnet

${ISAACLAB_PATH}/isaaclab.sh -p -m pip install -e .
${ISAACLAB_PATH}/isaaclab.sh -p -m pip install nepher

EVAL_REPO_URL="${EVAL_REPO_URL:-https://github.com/nepher-ai/eval-nav.git}"
git clone "${EVAL_REPO_URL}" ./eval-nav
${ISAACLAB_PATH}/isaaclab.sh -p -m pip install -e ./eval-nav
```

### Configure & Run

```bash
cp config/validator_config.example.yaml config/validator_config.yaml
nano config/validator_config.yaml  # Set API key + wallet

# GPU validator (default — full behaviour):
nepher-validator run --config config/validator_config.yaml

# CPU validator (weights & burn only — no GPU needed):
nepher-validator run --config config/validator_config.yaml --mode cpu
```

<details><summary><b>Run as systemd service</b></summary>

Create `/etc/systemd/system/nepher-validator.service`:

```ini
[Unit]
Description=Nepher Subnet 49 Validator
After=network.target

[Service]
Type=simple
User=root  # adjust to a dedicated service user if preferred
WorkingDirectory=/root/nepher-subnet
Environment="ISAACLAB_PATH=/path/to/isaac-lab"
Environment="ISAACSIM_PATH=/path/to/isaac-sim"
ExecStart=/path/to/isaac-lab/isaaclab.sh -p -m validator run --config /root/nepher-subnet/config/validator_config.yaml
Restart=unless-stopped
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now nepher-validator
journalctl -u nepher-validator -f
```
</details>

---

## 7. Health Check

```bash
python scripts/health_check.py   # All checks should show ✅
```

The script verifies: GPU availability, Isaac Sim/Lab paths, API key validity, wallet presence, subnet registration, chain connectivity, and Docker runtime (if applicable). If a specific check fails, refer to [Troubleshooting](#9-troubleshooting).

---

## 8. CPU/GPU Split Deployment

Split the workload across two machines so the expensive GPU is only rented during evaluation periods.

| Machine | Flag | Runs 24/7? | Responsibility |
|---|---|---|---|
| **CPU VPS** | `--mode cpu` | Yes | Set-weights during reward; burn on UID 0 every hour during all other periods |
| **GPU instance** | `--mode gpu` (default) | Only during evaluation | Setup + evaluation + score submission |

Both use the **same wallet, hotkey, and config**. Each independently polls the tournament API — no coordination channel needed.

### Docker

```bash
# On the CPU VPS:
docker compose up -d validator-cpu

# On the GPU machine (only during evaluation):
docker compose up -d validator
```

The `validator-cpu` service uses a lightweight image (`python:3.10-slim`, ~200 MB) — no Isaac Sim, no NVIDIA drivers. It builds in under a minute.

### Native

```bash
# CPU machine:
nepher-validator run --config config/validator_config.yaml --mode cpu

# GPU machine:
nepher-validator run --config config/validator_config.yaml
```

### Tips

- The GPU machine can be **stopped** after evaluation ends — the CPU validator handles all chain operations.
- If both happen to run during the reward period, they set identical weights. The overlap is harmless.
- You can also set `mode: cpu` in `validator_config.yaml` instead of passing the CLI flag every time.

---

## 9. Troubleshooting

| Issue | Fix |
|---|---|
| **GPU not detected (Docker)** | `sudo apt install -y nvidia-container-toolkit && sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker` |
| **Wallet not found** | Verify `~/.bittensor/wallets/validator/` has `coldkey`, `coldkeypub.txt`, `hotkeys/default`. Docker: check volume mount `~/.bittensor:/root/.bittensor:ro`. |
| **API key issues** | `grep api_key config/validator_config.yaml` — ensure key is present and correctly quoted. |
| **Isaac Lab/Sim not found** | Export `ISAACLAB_PATH` and `ISAACSIM_PATH` in `~/.bashrc`. |
| **Evaluation timeout** | Override: `retry.evaluation_timeout_seconds: 7200` in `validator_config.yaml`. |
| **Weight setting failures** | Auto-retries with backoff. Check: staked TAO, subnet 49 registration, chain connectivity. |
| **Docker build fails** | `docker compose build --no-cache validator` — ensure ≥ 20 GB free disk (`df -h`). |
| **Hangs after startup** | Test API connectivity: `curl -sI https://tournament-api.nepher.ai/api/v1/tournaments/active`. If DNS fails, add `dns: ["8.8.8.8"]` to `docker-compose.yaml`. |

---

## Need Help?

- **Docs:** https://docs.nepher.ai
- **Discord:** https://discord.gg/nepher
- **Issues:** https://github.com/nepher-ai/nepher-subnet/issues
