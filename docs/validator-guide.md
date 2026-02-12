# Nepher Validator Guide — From Scratch on a Rented GPU Machine

This guide walks you through every step needed to set up and run a **Nepher Subnet 49 validator** on a freshly rented GPU machine (e.g., RunPod, Vast.ai, Lambda, etc.).

---

## Table of Contents

1. [Prerequisites & Hardware Requirements](#1-prerequisites--hardware-requirements)
2. [Initial Server Setup](#2-initial-server-setup)
3. [Install NVIDIA Drivers & Container Toolkit](#3-install-nvidia-drivers--container-toolkit)
4. [Create a Bittensor Wallet](#4-create-a-bittensor-wallet)
5. [Get Your Nepher API Key](#5-get-your-nepher-api-key)
6. [Option A — Run with Docker (Recommended)](#6-option-a--run-with-docker-recommended)
7. [Option B — Run Natively (Without Docker)](#7-option-b--run-natively-without-docker)
8. [Configuration Reference](#8-configuration-reference)
9. [Health Check](#9-health-check)
10. [Monitoring & Logs](#10-monitoring--logs)
11. [Troubleshooting](#11-troubleshooting)

---

## 1. Prerequisites & Hardware Requirements

| Requirement | Minimum | Recommended |
|---|---|---|
| **GPU** | NVIDIA RTX A6000 | NVIDIA A100 (40 GB+) |
| **VRAM** | 24 GB | 40 GB+ |
| **RAM** | 32 GB | 64 GB+ |
| **Disk** | 100 GB SSD | 200 GB+ NVMe SSD |
| **OS** | Ubuntu 22.04 LTS | Ubuntu 22.04 LTS |
| **NVIDIA Driver** | 535+ | Latest stable |
| **CUDA** | 12.1+ | 12.1+ |

> **Tip:** Most GPU cloud providers (RunPod, Vast.ai, Lambda) come with NVIDIA drivers and Docker pre-installed. If yours does, you can skip directly to [Step 4](#4-create-a-bittensor-wallet).

### Software Requirements

- **Isaac Sim 5.1** — NVIDIA's robotics simulator
- **Isaac Lab 2.3.0** — NVIDIA's robot learning framework (built on Isaac Sim)
- **Python 3.10+**
- **Docker + Docker Compose** (for Docker-based setup)
- **Git**

---

## 2. Initial Server Setup

SSH into your rented machine and run basic setup:

```bash
# Update system packages
sudo apt update && sudo apt upgrade -y

# Install essential tools
sudo apt install -y git curl wget build-essential software-properties-common

# Verify GPU is detected
nvidia-smi
```

You should see your GPU listed with driver version and CUDA version. If `nvidia-smi` fails, you need to install NVIDIA drivers (see next step).

---

## 3. Install NVIDIA Drivers & Container Toolkit

> **Skip this step** if your cloud provider already has NVIDIA drivers and Docker installed (most do). Run `nvidia-smi` and `docker --version` to check.

### 3a. Install NVIDIA Drivers (if not pre-installed)

```bash
sudo apt install -y nvidia-driver-535
sudo reboot
# After reboot, verify:
nvidia-smi
```

### 3b. Install Docker (if not pre-installed)

```bash
# Install Docker
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh

# Add your user to docker group (avoids needing sudo)
sudo usermod -aG docker $USER
newgrp docker

# Verify
docker --version
```

### 3c. Install NVIDIA Container Toolkit

This allows Docker containers to access your GPU:

```bash
# Add NVIDIA container toolkit repository
distribution=$(. /etc/os-release;echo $ID$VERSION_ID)
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/$distribution/libnvidia-container.list | \
    sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
    sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

# Install
sudo apt update
sudo apt install -y nvidia-container-toolkit

# Configure Docker runtime
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker

# Verify GPU access in Docker
docker run --rm --gpus all nvidia/cuda:12.1.0-base-ubuntu22.04 nvidia-smi
```

### 3d. Install Docker Compose (if not pre-installed)

```bash
# Install Docker Compose plugin
sudo apt install -y docker-compose-plugin

# Verify
docker compose version
```

---

## 4. Create a Bittensor Wallet

You need a Bittensor wallet with a **coldkey** and a **hotkey** to run a validator.

### 4a. Install Bittensor CLI

```bash
pip install bittensor
```

### 4b. Create a New Wallet

```bash
# Create a new coldkey (this is your main wallet — SAVE THE MNEMONIC SECURELY)
btcli wallet new_coldkey --wallet.name validator

# Create a hotkey for the validator
btcli wallet new_hotkey --wallet.name validator --wallet.hotkey default
```

> **⚠️ IMPORTANT:** Back up your coldkey mnemonic phrase securely. If you lose it, you lose access to your wallet and staked TAO forever.

### 4c. Fund Your Wallet

Your validator wallet needs TAO for:
- **Registration** on Subnet 49
- **Staking** (validators need stake to have weight-setting permission)

Transfer TAO to your coldkey address:

```bash
# Check your coldkey address
btcli wallet overview --wallet.name validator
```

### 4d. Register on Subnet 49

```bash
btcli subnet register --wallet.name validator --wallet.hotkey default --netuid 49
```

### 4e. Stake TAO

```bash
btcli stake add --wallet.name validator --wallet.hotkey default --amount <AMOUNT>
```

### 4f. If Restoring an Existing Wallet

If you already have a wallet and are setting up on a new machine:

```bash
# Restore from mnemonic
btcli wallet regen_coldkey --wallet.name validator
btcli wallet regen_hotkey --wallet.name validator --wallet.hotkey default
```

Your wallet files will be stored at `~/.bittensor/wallets/validator/`.

---

## 5. Get Your Nepher API Key

1. Go to the **Nepher Tournament Platform**: https://tournament-api.nepher.ai
2. Sign in / register as a validator
3. Navigate to your **dashboard** or **API settings**
4. Generate or copy your **API key**

You can also join the **Discord** for support: https://discord.gg/nepher

---

## 6. Option A — Run with Docker (Recommended)

Docker is the easiest way to run the validator. It bundles Isaac Sim, Isaac Lab, and all dependencies.

### 6a. Clone the Repository

```bash
cd ~
git clone https://github.com/nepher-ai/nepher-subnet.git
cd nepher-subnet
```

### 6b. Set Up Environment Variables

```bash
# Copy the example env file
cp config/docker.env.example .env

# Edit with your values
nano .env
```

Set the following in your `.env` file:

```bash
# REQUIRED — Your Nepher API key
NEPHER_API_KEY=nepher_your_actual_api_key_here

# OPTIONAL — Custom API URL (default is fine for production)
# NEPHER_API_URL=https://tournament-api.nepher.ai

# OPTIONAL — Custom wallet path (default: ~/.bittensor)
# BITTENSOR_WALLET_PATH=~/.bittensor

# OPTIONAL — Wallet configuration (default: validator / default)
# WALLET_NAME=validator
# WALLET_HOTKEY=default
```

### 6c. Set Up Validator Config

```bash
# Copy the example config
cp config/validator_config.example.yaml config/validator_config.yaml

# Edit if you need to change defaults
nano config/validator_config.yaml
```

The key settings to verify:

```yaml
subnet:
  network: "finney"          # Use "finney" for mainnet, "test" for testnet
  subnet_uid: 49

tournament:
  api_key: "${NEPHER_API_KEY}"  # Reads from environment variable

wallet:
  name: "validator"             # Must match your wallet name from Step 4
  hotkey: "default"             # Must match your hotkey name from Step 4
```

### 6d. Build the Docker Image

```bash
docker compose build validator
```

> **Note:** This build can take **30–60 minutes** on first run as it downloads the Isaac Sim base image (~20 GB) and installs Isaac Lab.

### 6e. Start the Validator

```bash
# Run in the foreground (to see logs directly)
docker compose up validator

# OR run in the background (detached)
docker compose up -d validator
```

### 6f. Verify It's Running

```bash
# Check container status
docker compose ps

# View logs
docker compose logs -f validator

# Check health
docker compose exec validator bash -c '${ISAACLAB_PATH}/isaaclab.sh -p -c "import nepher_core; print(\"OK\")"'
```

You should see output like:

```
==============================================
Nepher Validator Container Starting
==============================================
Isaac Lab: /isaac-lab
Isaac Sim: /isaac-sim
==============================================
...
Nepher Validator Starting
Validator Hotkey: 5Gx...
Network: finney
Subnet UID: 49
...
No active tournament. Waiting...
```

### 6g. Managing the Docker Validator

```bash
# Stop the validator
docker compose down

# Restart the validator
docker compose restart validator

# View real-time logs
docker compose logs -f validator

# Shell into the container for debugging
docker compose exec validator bash
```

---

## 7. Option B — Run Natively (Without Docker)

If you prefer to install everything directly on the machine (not recommended for most users).

### 7a. Install Isaac Sim 5.1

Follow the [NVIDIA Isaac Sim installation guide](https://docs.omniverse.nvidia.com/isaacsim/latest/installation/install_workstation.html).

After installation:

```bash
# Set environment variables (add these to your ~/.bashrc)
export ISAACSIM_PATH=/path/to/isaac-sim
export ISAACLAB_PATH=/path/to/isaac-lab

# Verify
echo $ISAACSIM_PATH
echo $ISAACLAB_PATH
```

### 7b. Install Isaac Lab 2.3.0

```bash
git clone https://github.com/isaac-sim/IsaacLab.git $ISAACLAB_PATH
cd $ISAACLAB_PATH
git checkout v2.3.0
./isaaclab.sh --install
```

### 7c. Clone Nepher Subnet

```bash
cd ~
git clone https://github.com/nepher-ai/nepher-subnet.git
cd nepher-subnet
```

### 7d. Install Dependencies

Use Isaac Lab's Python environment to install dependencies:

```bash
# Install nepher-subnet and dependencies
${ISAACLAB_PATH}/isaaclab.sh -p -m pip install -e .

# Install the nepher (envhub) package (required for validators)
${ISAACLAB_PATH}/isaaclab.sh -p -m pip install nepher

# Clone and install eval-nav
git clone https://github.com/nepher-ai/eval-nav.git ./eval-nav
${ISAACLAB_PATH}/isaaclab.sh -p -m pip install -e ./eval-nav
```

### 7e. Configure

```bash
# Copy config
cp config/validator_config.example.yaml config/validator_config.yaml

# Edit with your settings
nano config/validator_config.yaml

# Set your API key
export NEPHER_API_KEY=nepher_your_actual_api_key_here
```

### 7f. Run the Validator

```bash
# Using the start script
export NEPHER_API_KEY=nepher_your_actual_api_key_here
./scripts/start_validator.sh --config config/validator_config.yaml

# OR using the CLI directly
nepher-validator run --config config/validator_config.yaml

# OR using Python module
python -m validator run --config config/validator_config.yaml

# With verbose logging
nepher-validator run --config config/validator_config.yaml --verbose

# With JSON logs (for production / log aggregation)
nepher-validator run --config config/validator_config.yaml --json-logs

# With a log file
nepher-validator run --config config/validator_config.yaml --log-file /var/log/nepher-validator.log
```

### 7g. Run as a Background Service (systemd)

For production, set up a systemd service so the validator auto-restarts:

```bash
sudo nano /etc/systemd/system/nepher-validator.service
```

Paste the following (adjust paths as needed):

```ini
[Unit]
Description=Nepher Subnet 49 Validator
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/nepher-subnet
Environment="NEPHER_API_KEY=nepher_your_actual_api_key_here"
Environment="ISAACLAB_PATH=/path/to/isaac-lab"
Environment="ISAACSIM_PATH=/path/to/isaac-sim"
ExecStart=/path/to/isaac-lab/isaaclab.sh -p -m validator run --config /root/nepher-subnet/config/validator_config.yaml
Restart=unless-stopped
RestartSec=30

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable nepher-validator
sudo systemctl start nepher-validator

# Check status
sudo systemctl status nepher-validator

# View logs
journalctl -u nepher-validator -f
```

---

## 8. Configuration Reference

The validator config file (`config/validator_config.yaml`) supports these options:

```yaml
# Subnet configuration
subnet:
  network: "finney"            # finney | test | local
  subnet_uid: 49               # Nepher subnet UID

# Tournament API
tournament:
  api_url: "https://tournament-api.nepher.ai"
  api_key: "${NEPHER_API_KEY}"  # Resolved from environment

# Wallet
wallet:
  name: "validator"             # Wallet name
  hotkey: "default"             # Hotkey name
  # path: "/custom/path"        # Optional custom wallet path

# Isaac Lab / Sim versions
isaac:
  lab_version: "2.3.0"
  sim_version: "5.1"

# Paths
paths:
  workspace: "./workspace"      # Working directory for evaluations
  eval_repo: "./eval-nav"       # Evaluation repository path
  env_cache: "~/.cache/nepher"  # Cached environments

# Retry settings
retry:
  network_max_attempts: 3          # API request retries
  network_initial_delay: 1.0       # Seconds
  network_max_delay: 30.0          # Seconds
  network_backoff_factor: 2.0
  evaluation_max_attempts: 2       # Evaluation retries per agent
  evaluation_timeout_seconds: 3600 # 1 hour per agent evaluation
  weight_setting_max_attempts: 5   # Weight-setting retries
  weight_setting_initial_delay: 5.0
```

### Environment Variables

Values in the config can reference environment variables using `${VAR}` or `${VAR:-default}` syntax.

| Variable | Description | Default |
|---|---|---|
| `NEPHER_API_KEY` | Tournament API key | **Required** |
| `WALLET_NAME` | Bittensor wallet name | `validator` |
| `WALLET_HOTKEY` | Bittensor hotkey name | `default` |
| `NEPHER_WORKSPACE` | Workspace directory | `./workspace` |
| `NEPHER_EVAL_REPO` | Eval repo path | `./eval-nav` |
| `NEPHER_ENV_CACHE` | Environment cache path | `~/.cache/nepher` |
| `ISAACLAB_PATH` | Isaac Lab installation path | — |
| `ISAACSIM_PATH` | Isaac Sim installation path | — |

---

## 9. Health Check

Before running the validator, verify everything is set up correctly:

```bash
# Run the built-in health check script
python scripts/health_check.py
```

Expected output when everything is configured:

```
==================================================
Nepher Subnet Health Check
==================================================

Checking Python version... ✅ Python 3.10.x
Checking nepher_core... ✅ Version x.x.x
Checking bittensor... ✅ Installed
Checking nepher (envhub)... ✅ Installed
Checking Isaac Lab... ✅ Found at /path/to/isaac-lab
Checking API key... ✅ Set (nepher_y...)
Checking wallet... ✅ Found validator/default

==================================================
✅ All checks passed (7/7)
```

If any checks fail, address them before starting the validator.

---

## 10. Monitoring & Logs

### Docker Setup

```bash
# Real-time logs
docker compose logs -f validator

# Last 100 lines
docker compose logs --tail 100 validator

# Container resource usage
docker stats
```

### Native Setup

```bash
# If using systemd
journalctl -u nepher-validator -f

# If using --log-file flag
tail -f /var/log/nepher-validator.log
```

### What to Watch For

| Log Message | Meaning |
|---|---|
| `No active tournament. Waiting...` | Normal — no tournament is running right now. Polls every 5 min. |
| `Contest period - waiting...` | Tournament is in contest phase. Validators wait. |
| `Starting validator setup phase` | Submit window started. Validator downloading configs & envs. |
| `Setup phase complete!` | Environments and configs ready for evaluation. |
| `Starting evaluation loop` | Evaluation period started. Processing submitted agents. |
| `Found X pending agents` | Agents are being evaluated. |
| `✅ Evaluation complete for agent: ...` | An agent was successfully evaluated. |
| `Starting reward phase` | Setting weights to the tournament winner. |
| `✅ Weights set successfully to UID X` | Weights committed on chain. |
| `Tournament completed` | Cycle done. Resets and waits for next tournament. |

---

## 11. Troubleshooting

### GPU Not Detected in Docker

```bash
# Verify NVIDIA runtime is configured
docker info | grep -i nvidia

# Test GPU access
docker run --rm --gpus all nvidia/cuda:12.1.0-base-ubuntu22.04 nvidia-smi

# If it fails, reconfigure the runtime:
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

### Wallet Not Found

```bash
# Check wallet exists
ls ~/.bittensor/wallets/validator/

# Should contain:
#   coldkey
#   coldkeypub.txt
#   hotkeys/default

# If using Docker, ensure the volume mount is correct in docker-compose.yaml:
#   - ~/.bittensor:/root/.bittensor:ro
```

### API Key Issues

```bash
# Verify the env var is set
echo $NEPHER_API_KEY

# If using Docker, check .env file is being loaded
docker compose config | grep NEPHER_API_KEY
```

### Isaac Lab / Sim Not Found (Native Setup)

```bash
# Verify environment variables
echo $ISAACLAB_PATH
echo $ISAACSIM_PATH

# Make sure they're in your ~/.bashrc for persistence
grep ISAAC ~/.bashrc

# If missing, add them:
echo 'export ISAACLAB_PATH=/path/to/isaac-lab' >> ~/.bashrc
echo 'export ISAACSIM_PATH=/path/to/isaac-sim' >> ~/.bashrc
source ~/.bashrc
```

### Evaluation Timeout

If agent evaluations are timing out (default: 1 hour), you can increase the timeout in your config:

```yaml
retry:
  evaluation_timeout_seconds: 7200  # 2 hours
```

### Weight Setting Failures

Weight setting can fail due to network congestion. The validator retries with exponential backoff automatically. If it still fails:

- Ensure your wallet has enough staked TAO
- Verify you're registered on Subnet 49: `btcli subnet list --netuid 49`
- Check network connectivity to Bittensor chain

### Docker Build Fails

```bash
# Clean build (no cache)
docker compose build --no-cache validator

# Check disk space (Isaac Sim image is ~20 GB)
df -h
```

### Container Keeps Restarting

```bash
# Check exit logs
docker compose logs --tail 50 validator

# Common causes:
# - Missing API key
# - Wallet not mounted properly
# - GPU not accessible
```

---

## Quick Reference — Cheat Sheet

```bash
# ──────────────────────────────────────────────
# DOCKER SETUP (Recommended)
# ──────────────────────────────────────────────

# 1. Clone
git clone https://github.com/nepher-ai/nepher-subnet.git && cd nepher-subnet

# 2. Configure
cp config/docker.env.example .env
cp config/validator_config.example.yaml config/validator_config.yaml
nano .env  # Set NEPHER_API_KEY

# 3. Build
docker compose build validator

# 4. Run
docker compose up -d validator

# 5. Check logs
docker compose logs -f validator

# ──────────────────────────────────────────────
# NATIVE SETUP
# ──────────────────────────────────────────────

# 1. Clone
git clone https://github.com/nepher-ai/nepher-subnet.git && cd nepher-subnet

# 2. Install
${ISAACLAB_PATH}/isaaclab.sh -p -m pip install -e .
${ISAACLAB_PATH}/isaaclab.sh -p -m pip install nepher

# 3. Configure
cp config/validator_config.example.yaml config/validator_config.yaml
export NEPHER_API_KEY=your_key_here

# 4. Run
nepher-validator run --config config/validator_config.yaml
```

---

## Need Help?

- **Documentation:** https://docs.nepher.ai
- **Discord:** https://discord.gg/nepher
- **GitHub Issues:** https://github.com/nepher-ai/nepher-subnet/issues

