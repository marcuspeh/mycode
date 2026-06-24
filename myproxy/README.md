# myproxy

Anthropic-compatible proxy server for MiniMax.

## Setup

```bash
cp .env.example .env
# Add your API key to .env

docker compose up -d
```

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/v1/messages` | Anthropic Messages API |
| `GET` | `/admin/models` | List configured models |
| `POST` | `/admin/reload` | Hot-reload models.yaml |
| `GET` | `/health` | Health check |

## Models

Edit `config/models.yaml` to configure models. The proxy auto-reloads changes.

```yaml
models:
  claude-minimax-3:
    provider: minimax
    model: MiniMax-M1

  claude-minimax-2.7:
    provider: minimax
    model: MiniMax-2.7
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `MINIMAX_API_KEY` | MiniMax API key |
| `MYPROXY_CONFIG_DIR` | Override config directory |

## Development

```bash
pip install -e ".[dev]"
pytest tests/
```
