# Nepher Validator Guide

Set up and run a **Nepher Subnet 49 validator** on a GPU machine (RunPod, Vast.ai, Lambda, etc.).

---

## Table of Contents

1. [Hardware & Software Requirements](#1-hardware--software-requirements)
2. [Initial Server Setup](#2-initial-server-setup)
3. [Bittensor Wallet](#3-bittensor-wallet)
4. [Get Your Nepher API Key](#4-get-your-nepher-api-key)
5. [Option A — Docker (Recommended)](#5-option-a--docker-recommended)
6. [Option B — Native Install](#6-option-b--native-install)
7. [Configuration Reference](#7-configuration-reference)
8. [Health Check](#8-health-check)
9. [Monitoring & Logs](#9-monitoring--logs)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. Hardware & Software Requirements

| Requirement | Minimum | Recommended |
|---|---|---|
| **GPU** | NVIDIA RTX A6000 | NVIDIA A100 (40 GB+) |
| **VRAM** | 24 GB | 40 GB+ |
| **RAM** | 32 GB | 64 GB+ |
| **Disk** | 100 GB SSD | 200 GB+ NVMe SSD |
| **OS** | Ubuntu 22.04 LTS | Ubuntu 22.04 LTS |
| **NVIDIA Driver** | 535+ | Latest stable |
| **CUDA** | 12.1+ | 12.1+ |

**Software:** Isaac Sim 5.1, Isaac Lab 2.3.0, Python 3.10+, Docker + Docker Compose, Git

> **Tip:** Most GPU cloud providers ship NVIDIA drivers and Docker pre-installed. If so, skip to [Step 3](#3-bittensor-wallet).

---

## 2. Initial Server Setup

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y git curl wget build-essential software-properties-common
nvidia-smi  # Verify GPU — if this fails, install drivers below
```

### Install NVIDIA Drivers (if needed)

```bash
sudo apt install -y nvidia-driver-535
sudo reboot
nvidia-smi  # Verify after reboot
```

### Install Docker (if needed)

```bash
curl -fsSL https://get.docker.com -o get-docker.sh && sudo sh get-docker.sh
sudo usermod -aG docker $USER && newgrp docker
```

### Install NVIDIA Container Toolkit (if needed)

```bash
distribution=$(. /etc/os-release;echo $ID$VERSION_ID)
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/$distribution/libnvidia-container.list | \
    sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
    sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

sudo apt update && sudo apt install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker

# Verify GPU access in Docker
docker run --rm --gpus all nvidia/cuda:12.1.0-base-ubuntu22.04 nvidia-smi
```

### Install Docker Compose (if needed)

```bash
sudo apt install -y docker-compose-plugin
```

---

## 3. Bittensor Wallet

### Create a New Wallet

```bash
pip install bittensor

btcli wallet new_coldkey --wallet.name validator
btcli wallet new_hotkey --wallet.name validator --wallet.hotkey default
```

> **⚠️ Back up your coldkey mnemonic securely.** Lost mnemonic = lost wallet + staked TAO.

### Fund, Register & Stake

```bash
btcli wallet overview --wallet.name validator                          # Get your coldkey address
btcli subnet register --wallet.name validator --wallet.hotkey default --netuid 49
btcli stake add --wallet.name validator --wallet.hotkey default --amount <AMOUNT>
```

### Restore an Existing Wallet (optional)

```bash
btcli wallet regen_coldkey --wallet.name validator
btcli wallet regen_hotkey --wallet.name validator --wallet.hotkey default
```

Wallet files are stored at `~/.bittensor/wallets/validator/`.

---

## 4. Get Your Nepher API Key

1. Go to **https://tournament-api.nepher.ai** — sign in / register
2. Navigate to **Dashboard → API Settings**
3. Copy your **API key**

Discord support: https://discord.gg/nepher

---

## 5. Option A — Docker (Recommended)

```bash
git clone https://github.com/nepher-ai/nepher-subnet.git && cd nepher-subnet
```

### Configure

```bash
cp config/docker.env.example .env
nano .env   # Optionally set WALLET_NAME, WALLET_HOTKEY, BITTENSOR_WALLET_PATH

cp config/validator_config.example.yaml config/validator_config.yaml
nano config/validator_config.yaml  # Set your API key and wallet name/hotkey
```

Key `config/validator_config.yaml` values:

```yaml
tournament:
  api_key: "nepher_your_actual_api_key_here"

wallet:
  name: "validator"
  hotkey: "default"
```

> **Note:** Shared settings (subnet, isaac, paths, retry) live in `config/common_config.yaml` which ships with the repo. Your `validator_config.yaml` only needs wallet and API key — the loader merges both files automatically.

### Build & Run

```bash
docker compose build validator          # First build takes 30–60 min (Isaac Sim ~20 GB)
docker compose up -d validator          # Start detached
docker compose logs -f validator        # Tail logs
```

### Manage

```bash
docker compose down                     # Stop
docker compose restart validator        # Restart
docker compose up -d --build validator  # Rebuild after code updates
docker compose logs -f validator        # Tail logs
docker compose exec validator bash      # Shell into container
```

---

## 6. Option B — Native Install

### Install Isaac Sim 5.1 & Isaac Lab 2.3.0

Follow the [NVIDIA Isaac Sim install guide](https://docs.omniverse.nvidia.com/isaacsim/latest/installation/install_workstation.html), then:

```bash
export ISAACSIM_PATH=/path/to/isaac-sim
export ISAACLAB_PATH=/path/to/isaac-lab

git clone https://github.com/isaac-sim/IsaacLab.git $ISAACLAB_PATH
cd $ISAACLAB_PATH && git checkout v2.3.0 && ./isaaclab.sh --install
```

### Install Nepher Subnet

```bash
cd ~ && git clone https://github.com/nepher-ai/nepher-subnet.git && cd nepher-subnet

${ISAACLAB_PATH}/isaaclab.sh -p -m pip install -e .
${ISAACLAB_PATH}/isaaclab.sh -p -m pip install nepher

# Clone eval repo (URL is configurable via EVAL_REPO_URL or paths.eval_repo_url in config)
EVAL_REPO_URL="${EVAL_REPO_URL:-https://github.com/nepher-ai/eval-nav.git}"
git clone "${EVAL_REPO_URL}" ./eval-nav
${ISAACLAB_PATH}/isaaclab.sh -p -m pip install -e ./eval-nav
```

### Configure & Run

```bash
cp config/validator_config.example.yaml config/validator_config.yaml
nano config/validator_config.yaml  # Set your API key and wallet name/hotkey

# Any of these work:
./scripts/start_validator.sh --config config/validator_config.yaml
nepher-validator run --config config/validator_config.yaml
python -m validator run --config config/validator_config.yaml

# Useful flags: --verbose, --json-logs, --log-file /var/log/nepher-validator.log
```

> Shared settings are loaded from `config/common_config.yaml` alongside your `validator_config.yaml` automatically.

### Run as systemd Service (optional)

Create `/etc/systemd/system/nepher-validator.service`:

```ini
[Unit]
Description=Nepher Subnet 49 Validator
After=network.target

[Service]
Type=simple
User=root
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

---

## 7. Configuration Reference

Configuration is split into two layers:

| File | Purpose | Checked in? |
|---|---|---|
| `config/common_config.yaml` | Shared / project-level settings | **Yes** (static) |
| `config/validator_config.yaml` | User-specific settings (wallet, API key) | No (`.gitignore`d) |

The config loader automatically merges both files — user values override common values.

### `config/common_config.yaml` (static, ships with repo)

```yaml
subnet:
  network: "finney"
  subnet_uid: 49

tournament:
  api_url: "https://tournament-api.nepher.ai"

isaac:
  lab_version: "2.3.0"
  sim_version: "5.1"

paths:
  workspace: "./workspace"
  eval_repo: "./eval-nav"
  eval_repo_url: "https://github.com/nepher-ai/eval-nav.git"
  env_cache: "~/.cache/nepher"

retry:
  network_max_attempts: 3
  network_initial_delay: 1.0
  network_max_delay: 30.0
  network_backoff_factor: 2.0
  evaluation_max_attempts: 2
  evaluation_timeout_seconds: 3600
  weight_setting_max_attempts: 5
  weight_setting_initial_delay: 5.0
```

### `config/validator_config.yaml` (user creates from example)

```yaml
tournament:
  api_key: "your_api_key_here"

wallet:
  name: "validator"
  hotkey: "default"
```

The API key is set **directly** in `validator_config.yaml` — no environment variable needed.

Config values support `${VAR}` and `${VAR:-default}` environment variable substitution.

| Variable | Description | Default |
|---|---|---|
| `WALLET_NAME` | Bittensor wallet name | `validator` |
| `WALLET_HOTKEY` | Bittensor hotkey name | `default` |
| `NEPHER_WORKSPACE` | Workspace directory | `./workspace` |
| `NEPHER_EVAL_REPO` | Eval repo local path | `./eval-nav` |
| `EVAL_REPO_URL` | Eval repo Git URL | `https://github.com/nepher-ai/eval-nav.git` |
| `NEPHER_ENV_CACHE` | Environment cache path | `~/.cache/nepher` |
| `ISAACLAB_PATH` | Isaac Lab installation path | — |
| `ISAACSIM_PATH` | Isaac Sim installation path | — |

---

## 8. Health Check

```bash
python scripts/health_check.py
```

All 7 checks (Python version, nepher_core, bittensor, nepher envhub, Isaac Lab, API key, wallet) should show ✅.

---

## 9. Monitoring & Logs

```bash
# Docker
docker compose logs -f validator
docker compose logs --tail 100 validator
docker stats

# Native / systemd
journalctl -u nepher-validator -f
tail -f /var/log/nepher-validator.log
```

### Key Log Messages

| Message | Meaning |
|---|---|
| `Entering main loop ...` | Validator initialized, polling started. |
| `No active tournament. Sleeping 300s...` | Normal — no tournament running. Polls every 5 min. |
| `Active tournament found: ...` | Tournament detected; validator will act. |
| `Starting evaluation loop` / `Found X pending agents` | Evaluating submitted agents. |
| `✅ Evaluation complete for agent: ...` | Agent evaluated successfully. |
| `✅ Weights set successfully to UID X` | Weights committed on-chain. |
| `Tournament completed` | Cycle done; waiting for next tournament. |
| `[iter N] Main loop error: ...` | Error occurred; retries in 60 s. |

---

## 10. Troubleshooting

### CUDA / GPU Not Detected in Docker

```bash
nvidia-smi                                     # Host GPU OK?
docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi  # GPU in Docker?

# If the above fails:
sudo apt install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
docker compose up -d --build validator
```

> The entrypoint runs a GPU pre-flight check — the container will exit immediately with a clear message if CUDA is inaccessible.

### Wallet Not Found

Ensure `~/.bittensor/wallets/validator/` contains `coldkey`, `coldkeypub.txt`, and `hotkeys/default`. For Docker, confirm the volume mount `~/.bittensor:/root/.bittensor:ro` in `docker-compose.yaml`.

### API Key Issues

```bash
grep api_key config/validator_config.yaml     # Key present in config?
docker compose exec validator cat /app/config/validator_config.yaml  # Mounted correctly?
```

### Isaac Lab / Sim Not Found (Native)

Ensure `ISAACLAB_PATH` and `ISAACSIM_PATH` are exported in `~/.bashrc`.

### Evaluation Timeout

Increase in `common_config.yaml` (or override in your `validator_config.yaml`): `retry.evaluation_timeout_seconds: 7200`

### Weight Setting Failures

Retries are automatic with exponential backoff. If persistent, verify: sufficient staked TAO, registration on Subnet 49 (`btcli subnet list --netuid 49`), and chain connectivity.

### Docker Build Fails

```bash
docker compose build --no-cache validator
df -h   # Isaac Sim image is ~20 GB — check disk space
```

### Validator Hangs After Startup Banner

```bash
docker compose up -d --build validator        # Rebuild for latest logging
docker compose exec validator bash -c "curl -sI https://tournament-api.nepher.ai/api/v1/tournaments/active"
# If DNS fails, add to docker-compose.yaml: dns: ["8.8.8.8"]
```

---

## Need Help?

- **Docs:** https://docs.nepher.ai
- **Discord:** https://discord.gg/nepher
- **Issues:** https://github.com/nepher-ai/nepher-subnet/issues
