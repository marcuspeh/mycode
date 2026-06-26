# myproxy + mycode

Use the **Claude Code CLI** (`claude` command-line tool, not the IDE extension)
with MiniMax or DeepSeek via a simple Anthropic-compatible proxy.

**Architecture:**

```
Macbook / Laptop
    ↓
mycode --model minimax-3
    ↓
Claude Code
    ↓
ANTHROPIC_BASE_URL=http://genmachine:3566
    ↓
Tailscale
    ↓
Ubuntu server (Docker)
    ↓
myproxy
    ↓
MiniMax / DeepSeek
```

## Quick Start

### Server (Ubuntu)

```bash
cd myproxy
cp .env.example .env
# Edit .env with your API keys

docker compose up -d
```

### Client (Macbook / Laptop)

```bash
# Set machine name (optional)
export MYCODE_MACHINE=macbook

# Interactive picker — fetches available models from myproxy
mycode

# Or pick directly
mycode --model minimax-m3
mycode --model deepseek-v4-pro
```

## Project Structure

- [myproxy/](myproxy/README.md) — Proxy server (FastAPI, runs on Ubuntu)
- [mycode/](mycode/README.md) — CLI wrapper that launches the `claude` CLI (runs on client machines)
