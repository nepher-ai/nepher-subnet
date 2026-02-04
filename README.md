# Nepher Subnet

**Bittensor Subnet 49 - Robotics Tournament Platform**

Nepher is a decentralized robotics tournament platform on Bittensor that enables miners to submit trained navigation policies for evaluation by validators using standardized Isaac Lab environments.

## Overview

### Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     SHARED CORE LIBRARY                      │
├─────────────────────────────────────────────────────────────┤
│  nepher_core/                                                │
│  ├── api/          # Tournament API client                   │
│  ├── config/       # Configuration management                │
│  ├── wallet/       # Bittensor wallet utilities              │
│  └── utils/        # Common utilities                        │
└─────────────────────────────────────────────────────────────┘
           │                              │
           ▼                              ▼
    ┌─────────────┐              ┌─────────────────┐
    │    MINER    │              │    VALIDATOR    │
    │  (thin CLI) │              │ (evaluation +   │
    │             │              │  weight logic)  │
    └─────────────┘              └─────────────────┘
```

### Tournament Cycle

```
┌─────────────┬─────────────┬─────────────┬─────────────┬─────────────────┐
│   CONTEST   │    GRACE    │ EVALUATION  │   REVIEW    │   SETTLEMENT    │
│   PERIOD    │   WINDOW    │   PERIOD    │   STAGE     │    PERIOD       │
├─────────────┼─────────────┼─────────────┼─────────────┼─────────────────┤
│ Miners      │ Eligibility │ Validators  │ Admin       │ Winner gets     │
│ submit      │ snapshot    │ evaluate    │ reviews     │ all weights     │
│ agents      │ locked      │ agents      │ results     │                 │
└─────────────┴─────────────┴─────────────┴─────────────┴─────────────────┘
```

## Quick Start

### For Miners

1. **Install the package:**
   ```bash
   pip install nepher-subnet
   ```

2. **Train your agent** locally using the evaluation environments

3. **Submit your agent:**
   ```bash
   nepher-miner submit \
       --path ./my-agent \
       --wallet-name miner \
       --wallet-hotkey default \
       --api-key your_api_key
   ```

### For Validators

1. **Prerequisites:**
   - NVIDIA GPU (RTX 4090+ recommended)
   - Isaac Lab 2.3.0 + Isaac Sim 5.1
   - Docker with NVIDIA Container Toolkit

2. **Configure:**
   ```bash
   cp config/validator_config.example.yaml config/validator_config.yaml
   # Edit with your settings
   ```

3. **Run with Docker:**
   ```bash
   export NEPHER_API_KEY=your_api_key
   docker-compose up validator
   ```

4. **Or run natively:**
   ```bash
   nepher-validator run --config config/validator_config.yaml
   ```

## Installation

### From Source

```bash
git clone https://github.com/nepher-ai/nepher-subnet.git
cd nepher-subnet
pip install -e .
```

### For Development

```bash
pip install -e ".[dev]"
```

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `NEPHER_API_KEY` | Tournament API key | Required |
| `NEPHER_API_URL` | Tournament API URL | https://tournament.nepher.ai |
| `WALLET_NAME` | Bittensor wallet name | validator/miner |
| `WALLET_HOTKEY` | Bittensor hotkey name | default |
| `NEPHER_WORKSPACE` | Workspace directory | ./workspace |

### Validator Configuration

See `config/validator_config.example.yaml` for full configuration options.

## Agent Structure

Submitted agents must follow this structure:

```
my-agent/
├── best_policy/
│   └── best_policy.pt          # Trained policy (REQUIRED)
├── scripts/
│   ├── list_envs.py            # Environment verification
│   └── rsl_rl/
│       └── play.py             # Policy inference
└── source/
    └── <task_module>/          # e.g., leatherbacknav
        ├── __init__.py
        └── tasks/              # Task definitions
```

## Docker

### Build Images

```bash
# Miner (lightweight)
docker build -f docker/Dockerfile.miner -t nepher-miner .

# Validator (GPU required)
docker build -f docker/Dockerfile.validator -t nepher-validator .
```

### Run with Docker Compose

```bash
# Set environment
export NEPHER_API_KEY=your_api_key

# Run validator
docker-compose up validator

# Submit agent
docker-compose run miner submit --path /app/agent --api-key $NEPHER_API_KEY
```

## Development

### Running Tests

```bash
pytest tests/ -v --cov
```

### Code Quality

```bash
# Lint
ruff check .

# Type check
mypy nepher_core miner validator
```

## API Reference

### Miner CLI

```bash
# Submit agent
nepher-miner submit --path ./agent --wallet-name miner --api-key KEY

# Validate agent structure
nepher-miner validate --path ./agent
```

### Validator CLI

```bash
# Run validator
nepher-validator run --config ./config/validator_config.yaml

# With verbose logging
nepher-validator run --config ./config/validator_config.yaml --verbose

# With JSON logs (production)
nepher-validator run --config ./config/validator_config.yaml --json-logs
```

## License

MIT License - see [LICENSE](LICENSE) for details.

## Links

- **Website:** https://nepher.ai
- **Documentation:** https://docs.nepher.ai
- **Tournament Platform:** https://tournament.nepher.ai
- **Discord:** https://discord.gg/nepher

