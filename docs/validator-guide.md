# Validator Guide

**Nepher Robotics · Bittensor Subnet 49**

This guide walks through deploying a **Bittensor Subnet 49 validator** using Docker. A single GPU machine runs the complete validator lifecycle: agent evaluation, on-chain weight setting, and burn.

**How it works:** the `validator` is a lightweight orchestrator. It downloads miner agents, sets weights on-chain, and spawns a GPU **`sandbox`** container (Isaac Sim) on demand to evaluate untrusted agent code in isolation. The sandbox is built once but never runs as a long-lived service — the validator launches it via the host Docker socket. That's why the machine needs a GPU + the NVIDIA Container Toolkit even though the orchestrator itself is light.

> No GPU? You can run a **CPU-only validator** that sets weights but does not evaluate — see the [Appendix](#appendix-cpu-only-validator-weights-only).

---

## 1. Requirements

| Spec | Minimum | Recommended |
|---|---|---|
| **GPU** | RTX A6000 (24 GB VRAM) | A100 (40 GB+) |
| **RAM** | 32 GB | 64 GB+ |
| **Disk** | 100 GB SSD | 200 GB+ NVMe |
| **OS** | Ubuntu 22.04 LTS | Ubuntu 22.04 LTS |
| **NVIDIA Driver / CUDA** | 535+ / 12.1+ | Latest stable / 12.1+ |

**Software:** Docker + Compose, Git. Most GPU cloud providers ship drivers and Docker pre-installed — skip to [Step 3](#3-bittensor-wallet) if so.

---

## 2. Server Setup (if needed)

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y git curl wget build-essential
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
sudo apt install -y docker-compose-plugin

# NVIDIA Container Toolkit
distribution=$(. /etc/os-release;echo $ID$VERSION_ID)
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/$distribution/libnvidia-container.list | \
    sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
    sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt update && sudo apt install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker

# Verify
docker run --rm --gpus all nvidia/cuda:12.1.0-base-ubuntu22.04 nvidia-smi
```
</details>

---

## 3. Bittensor Wallet

Install the Bittensor CLI ([`btcli`](https://docs.bittensor.com/)), then register and stake on Subnet 49:

```bash
btcli subnet register --wallet.name validator --wallet.hotkey default --netuid 49
btcli stake add       --wallet.name validator --wallet.hotkey default --amount <AMOUNT>
```

Wallet files live at `~/.bittensor/wallets/validator/`.

---

## 4. Get Your Nepher API Key

Sign in at **https://account.nepher.ai** → **API Keys** → copy your key.

---

## 5. Configure & Run (Docker)

```bash
git clone https://github.com/nepher-ai/nepher-subnet.git && cd nepher-subnet

cp config/docker.env.example .env
cp config/validator_config.example.yaml config/validator_config.yaml
```

Edit **`.env`** (Docker Compose reads these — the wallet directory defaults to `~/.bittensor`):

```bash
NEPHER_API_KEY=nepher_your_actual_api_key_here
# Optional overrides (sensible defaults ship in docker-compose.yaml):
# BITTENSOR_WALLET_PATH=~/.bittensor
# NEPHER_API_URL=https://tournament-api.nepher.ai
```

Edit **`config/validator_config.yaml`** (read by the validator process — keep it in sync with `.env`):

```yaml
tournament:
  api_key: "nepher_your_actual_api_key_here"
wallet:
  name: "validator"
  hotkey: "default"
```

> Shared settings live in `config/common_config.yaml` (ships with repo) and are merged automatically.

Build the images, then start the validator. The first build is heavy because it builds the GPU `sandbox` image (Isaac Sim ~20 GB); the validator won't start until it completes:

```bash
docker compose build                    # Builds validator + sandbox — 30–60 min first time
docker compose up -d validator          # Starts the orchestrator (spawns sandbox on demand)
docker compose logs -f validator

# Manage
docker compose down                     # Stop
docker compose restart validator        # Restart
docker compose up -d --build validator  # Rebuild after updates
docker compose exec validator bash      # Shell into container
```

Check status — the validator has a built-in health check (`STATUS` shows `healthy` once it's up):

```bash
docker compose ps
```

---

## Appendix: CPU-Only Validator (weights only)

The `validator-cpu` service is a stripped-down validator that **only sets weights and burns** — it does **not** evaluate agents. Use it if you want to run a valid validator without a GPU. (The full GPU `validator` from Section 5 already does everything, including weight setting; the CPU service is not a companion to it.)

It runs on a cheap CPU VPS — 2 vCPU · 4 GB RAM · 20 GB disk · Ubuntu 22.04 — with no GPU, no Isaac Sim, and a lightweight image (~200 MB). It uses the same wallet, config, and `.env` setup as the GPU validator (Steps 3–5).

```bash
docker compose build validator-cpu && docker compose up -d validator-cpu
docker compose logs -f validator-cpu
```

---

## Need Help?

- **Docs:** https://docs.nepher.ai
- **Discord:** https://discord.gg/qZUc3vdjVq
- **Issues:** https://github.com/nepher-ai/nepher-subnet/issues
