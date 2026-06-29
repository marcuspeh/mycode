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

## Supported request features

The `/v1/messages` endpoint accepts the full Anthropic Messages request shape and forwards the following spec fields to the upstream provider:

| Field | Notes |
|-------|-------|
| `model`, `messages`, `system`, `max_tokens`, `temperature`, `stream` | Always forwarded |
| `tools`, `tool_choice` | Forwarded; `tool_choice` is restricted to `auto` / `none` per spec |
| `top_p`, `service_tier` | Forwarded when set |
| `thinking` | Forwarded to MiniMax only for `MiniMax-M3`; ignored otherwise (M2.x always thinks) |
| `cache_control` | Preserved on content blocks, system blocks, and tool definitions |
| `source` (image / video) | Preserved on content blocks (MiniMax-M3 only) |
| `metadata.user_id` | Forwarded as-is |

The response side preserves the full usage block: `input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`.

Streaming responses are reshaped into Anthropic-shaped SSE events (`message_start`, `content_block_start`, `content_block_delta`, `content_block_stop`, `message_delta`, `message_stop`, plus `signature_delta` for thinking blocks).

## Models

Edit `config/models.yaml` to configure models. The proxy auto-reloads changes.

```yaml
models:
  claude-lb-minimax-m3:
    provider: minimax
    model: MiniMax-M3
    context_length: 1000000

  claude-lb-deepseek-v4-pro:
    provider: deepseek
    model: deepseek-v4-pro
    context_length: 1000000
```

The `context_length` field is informational — it's exposed via the `X-Context-Length` response header so clients can budget their requests.

## Environment Variables

| Variable | Description |
|----------|-------------|
| `MINIMAX_API_KEYS` | Comma-separated MiniMax API keys (round-robin). Provider is enabled if any key is set. |
| `DEEPSEEK_API_KEYS` | Comma-separated DeepSeek API keys (round-robin). Provider is enabled if any key is set. |
| `MYPROXY_CONFIG_DIR` | Override config directory |
| `MYPROXY_HOST` | Bind host (default `0.0.0.0`) |
| `MYPROXY_PORT` | Bind port (default `3566`) |

### Key rotation

When more than one key is configured for a provider, the proxy round-robins
across them — each `/v1/messages` request uses the next key in the list,
wrapping around at the end. This distributes load (and per-key rate limits)
across multiple credentials.

```bash
# .env
MINIMAX_API_KEYS=sk-account-a,sk-account-b,sk-account-c
```

Behavior:
- A single configured key works exactly as before — no wrapping.
- Multiple keys are rotated in the order they appear in the env var.
- Whitespace around keys is stripped; blank entries (`sk-a,,sk-b`) are ignored.
- If a request fails with a connection / timeout / protocol error, the proxy
  retries it transparently with the next key. Once all keys fail, the most
  recent error is returned to the client.
- 4xx responses (including 401/429) are *not* retried — the upstream
  received the request and chose to reject it.

## Development

### Using uv

```bash
uv sync
uv run myproxy
```

### Running tests

```bash
uv run pytest
```

The suite covers model registry, scrubber, both providers, the `/v1/messages` endpoint (streaming and non-streaming), the SSE rewriter, and the API-key rotation wrapper.

