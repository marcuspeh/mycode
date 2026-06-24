# myproxy + mycode

Use Claude Code with MiniMax via a simple Anthropic-compatible proxy.

**Architecture:**

```
Macbook / Laptop
    ↓
mycode --model minimax-3
    ↓
Claude Code
    ↓
ANTHROPIC_BASE_URL=http://genmachine:8000
    ↓
Tailscale
    ↓
Ubuntu server (Docker)
    ↓
myproxy
    ↓
MiniMax
```

## Quick Start

### Server (Ubuntu)

```bash
cd myproxy
cp .env.example .env
# Edit .env with your API key

docker compose up -d
```

### Client (Macbook / Laptop)

```bash
# Set machine name (optional)
export MYCODE_MACHINE=macbook

# Launch Claude Code with MiniMax
mycode --model minimax-3
```

## Project Structure

- [myproxy/](myproxy/README.md) — Proxy server (FastAPI, runs on Ubuntu)
- [mycode/](mycode/README.md) — CLI wrapper (runs on client machines)
