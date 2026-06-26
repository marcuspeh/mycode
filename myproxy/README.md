# myproxy

Anthropic-compatible proxy server for MiniMax and DeepSeek.

## Setup

```bash
cp .env.example .env
# Add your API key(s) to .env

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

  claude-deepseek3:
    provider: deepseek
    model: deepseek-chat
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `MINIMAX_API_KEY` | MiniMax API key |
| `DEEPSEEK_API_KEY` | DeepSeek API key |
| `MYPROXY_CONFIG_DIR` | Override config directory |

## Development

```bash
pip install -e ".[dev]"
pytest tests/
```
