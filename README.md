# A.R.C.H.I.E. Node

Fleet compute node for the A.R.C.H.I.E. platform. Self-registers with hub, reports hardware, heartbeats every 30s.

## Quick Start

```bash
docker run -d --name archie-node --restart unless-stopped \
  --gpus all \
  -e HUB_URL=http://your-hub:3000 \
  -e TOKEN=your-registration-token \
  -p 3001:3001 \
  -v archie-node-data:/data \
  ghcr.io/kytrankatarn/archie-node:latest
```

**Without GPU:** Remove `--gpus all`.

## How It Works

1. Container starts, detects hardware (CPU, RAM, GPU)
2. Registers with hub using TOKEN for instant approval
3. Heartbeats every 30s with system metrics
4. Local API on port 3001 for health checks
5. Connects to local Ollama (if installed) for LLM dispatch

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `HUB_URL` | Yes | - | Hub URL (e.g., `http://192.168.1.200:3000`) |
| `TOKEN` | No | - | Registration token for auto-approval |
| `NODE_NAME` | No | hostname | Display name in hub |
| `OLLAMA_HOST` | No | `http://host.docker.internal:11434` | Ollama API URL |
| `HEARTBEAT_INTERVAL` | No | `30` | Seconds between heartbeats |
| `NODE_API_PORT` | No | `3001` | Local API port |

## Requirements

- Docker Desktop (Windows/Mac) or Docker Engine (Linux)
- Optional: NVIDIA GPU + drivers for GPU workloads
- Optional: Ollama installed separately for LLM inference

## License

Apache 2.0 — Kytran Empowerment Inc.
