# Nepher Robotics Subnet

**Bittensor Subnet 49 — Decentralized Robotics Tournament Platform**

Miners submit trained policies; validators score them inside isolated GPU **sandbox** containers running Isaac Lab. The tournament winner takes all weights.

## Architecture

```
  nepher_core/          Shared library (API client, config, wallet, utils)
       │
  ┌────┴────┐
  ▼         ▼
miner/    validator/
(submit)  (evaluate + set weights)
               │
               ▼  docker run (per agent, ephemeral)
          nepher-sandbox
          (Isaac Sim 5.1 + Isaac Lab — GPU)
```

The validator is a **lightweight orchestrator** — it never runs Isaac Sim directly. For each agent, it spawns an ephemeral `nepher-sandbox` container (~20 GB image), mounts the agent and benchmark environments read-only, runs evaluation, then removes the container. This isolates untrusted miner code from the validator wallet and host network.

> **First build note:** The sandbox image takes 30–60 min to build (~20 GB, Isaac Sim base).

## Sandbox Security

Sandbox containers run with:
- All capabilities dropped; no Docker socket or wallet access
- Read-only agent + environment mounts; writable output dir only
- GPU access for Isaac Sim (`--gpus all`)
- Network restricted to an **API-configured whitelist** via a transparent SNI proxy (iptables + `sni-proxy.py`); `NET_ADMIN` is dropped before miner code executes

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

docker compose build          # builds validator + sandbox (~20 GB, first run)
docker compose up -d validator
```

→ Full guide: [docs/validator-guide.md](docs/validator-guide.md)

## Configuration

Two-layer config — loader merges both automatically (user values override shared defaults):

| File | Purpose |
|---|---|
| `config/common_config.yaml` | Shared defaults (ships with repo) |
| `config/validator_config.yaml` / `miner_config.yaml` | Your wallet + API key (`.gitignore`d) |

## CLI Reference

```bash
# Miner
nepher-miner submit   --path ./agent --config config/miner_config.yaml
nepher-miner validate --path ./agent

# Validator
nepher-validator run --config config/validator_config.yaml [--verbose] [--json-logs]
```

## External Services

| Service | URL | Role |
|---|---|---|
| Tournament API | https://tournament-api.nepher.ai | Tournaments, agents, scores, winners |
| EnvHub | https://envhub-api.nepher.ai | Benchmark environment downloads |
| eval-nav | github.com/nepher-ai/eval-nav | Evaluation harness (baked into sandbox image) |

## Links

- **Website:** https://nepher.ai
- **Docs:** https://docs.nepher.ai
- **Discord:** https://discord.gg/nepher

## License

MIT — see [LICENSE](LICENSE).
