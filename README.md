# Nepher Robotics Subnet

**Bittensor Subnet 49 — Decentralized Robotics Tournament Platform**

Miners submit trained policies; validators evaluate them in standardized Isaac Lab environments. The tournament winner receives all weights.

## Architecture

```
  nepher_core/          Shared library (API client, config, wallet, utils)
       │
  ┌────┴────┐
  ▼         ▼
miner/    validator/
(submit)  (evaluate + set weights)
```

## Quick Start

### Miners

```bash
git clone https://github.com/nepher-ai/nepher-subnet.git && cd nepher-subnet
pip install -e .

cp config/miner_config.example.yaml config/miner_config.yaml
# Edit: set wallet + API key

nepher-miner submit --path ./my-agent --config config/miner_config.yaml
```

→ Full guide: [docs/miner-guide.md](docs/miner-guide.md)

### Validators (GPU)

Requires NVIDIA GPU (A100+ recommended), Isaac Sim 5.1, Isaac Lab 2.3.0, Docker + NVIDIA Container Toolkit.

```bash
git clone https://github.com/nepher-ai/nepher-subnet.git && cd nepher-subnet

cp config/docker.env.example .env
cp config/validator_config.example.yaml config/validator_config.yaml
# Edit: set wallet + API key

docker compose build validator
docker compose up -d validator
```

→ Full guide: [docs/validator-guide.md](docs/validator-guide.md)

### Validators (CPU — No GPU Required)

A lightweight alternative (`~200 MB` image, no Isaac Sim, no NVIDIA drivers) that handles **weight-setting and burning only**. Use this on a cheap VPS to keep your validator online 24/7 while reserving the GPU machine solely for evaluation windows.

```bash
git clone https://github.com/nepher-ai/nepher-subnet.git && cd nepher-subnet

cp config/docker.env.example .env
cp config/validator_config.example.yaml config/validator_config.yaml
# Edit: set wallet + API key

docker compose build validator-cpu
docker compose up -d validator-cpu
```

Or without Docker:

```bash
pip install -e .
nepher-validator run --config config/validator_config.yaml --mode cpu
```

> **CPU/GPU split deployment:** run `validator-cpu` on a cheap VPS for 24/7 weight-setting and burn, and only spin up the full GPU validator during evaluation. See the [validator guide](docs/validator-guide.md#8-cpugpu-split-deployment).

→ Full guide: [docs/validator-guide.md](docs/validator-guide.md)

## Agent Structure

```
my-agent/
├── best_policy/
│   └── best_policy.pt            # Trained policy (required)
├── scripts/
│   ├── list_envs.py
│   └── rsl_rl/
│       └── play.py               # Policy inference
└── source/
    └── <task_module>/
        └── tasks/
```

## Configuration

Two-layer config — the loader merges both automatically (user values override shared defaults):

| File | Purpose |
|---|---|
| `config/common_config.yaml` | Shared defaults (ships with repo) |
| `config/validator_config.yaml` / `miner_config.yaml` | Your wallet + API key (`.gitignore`d) |

## CLI Reference

```bash
# Miner
nepher-miner submit   --path ./agent --config config/miner_config.yaml
nepher-miner validate --path ./agent

# Validator — GPU (default, full evaluation + weight-setting)
nepher-validator run --config config/validator_config.yaml [--verbose] [--json-logs]

# Validator — CPU (weight-setting & burn only, no GPU needed)
nepher-validator run --config config/validator_config.yaml --mode cpu

# Validator — CPU via Docker (recommended for 24/7 VPS deployment)
docker compose build validator-cpu && docker compose up -d validator-cpu
```

## Development

```bash
pip install -e ".[dev]"
pytest tests/ -v --cov
ruff check .
mypy nepher_core miner validator
```

## Links

- **Website:** https://nepher.ai
- **Docs:** https://docs.nepher.ai
- **Tournament:** https://tournament-api.nepher.ai
- **Discord:** https://discord.gg/nepher

## License

MIT — see [LICENSE](LICENSE).
