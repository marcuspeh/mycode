# myproxy

Anthropic-compatible proxy server for MiniMax and DeepSeek.

## Setup

Everything is configured through a `.env` file in this directory. Copy the
template and fill in your provider keys:

```bash
cp .env.example .env
$EDITOR .env
```

```bash
# .env
MINIMAX_API_KEYS=sk-your-minimax-key
DEEPSEEK_API_KEYS=sk-your-deepseek-key
```

`MINIMAX_API_KEYS` and `DEEPSEEK_API_KEYS` are comma-separated lists — the
proxy round-robins across them. A provider is enabled only when its list is
non-empty, so you can set one and leave the other blank.

Then start the server:

```bash
docker compose up -d     # container
# or
uv run myproxy           # local
```

Verify it came up:

```bash
curl http://127.0.0.1:3566/health
curl http://127.0.0.1:3566/admin/models
```

The `.env` file is read directly by the app when running locally, and by
`docker compose` via `env_file` when running in a container. Only the
variables listed in the table below have an effect — anything else in the
file is ignored.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/v1/messages` | Anthropic Messages API |
| `GET` | `/v1/models` | List models (Anthropic `/v1/models` format) |
| `GET` | `/admin/models` | List configured models |
| `POST` | `/admin/reload` | Hot-reload models.yaml |
| `GET` | `/admin/stats` | Per-request rollups (tokens, cost, latency) |
| `GET` | `/admin/recent` | Tail of recent request trace lines |
| `GET` | `/health` | Health check |

## Using with Trae (custom model)

Trae's **Settings → Models → Add Model → Custom Model** can point at this
proxy. Choose **API Format: Anthropic Messages** — Trae then appends
`/v1/messages` to the URL you provide.

| Field | Value |
|-------|-------|
| API Format | Anthropic Messages |
| Custom Request URL | `http://<proxy-host>:3566` (Full URL **off**) — Trae appends `/v1/messages` |
| Model ID | A registry alias, e.g. `claude-lb-minimax-m3` |
| API Key | Any non-empty value — the proxy authenticates to the upstream provider, not the client |

With **Full URL** on, use `http://<proxy-host>:3566/v1/messages` instead.

The proxy ignores the client's `x-api-key` and `anthropic-version` headers,
so any placeholder key works.

Notes:

- Trae streams by default. The proxy emits Anthropic-spec SSE frames
  (`message_start`, `content_block_start`, `content_block_delta`,
  `message_delta`, `message_stop`), one JSON payload per frame.
- `tool_choice` supports `auto`, `any`, `none` and `tool` (with `name`).
- `GET /v1/models` lists the registry aliases so clients that probe for
  models find them.
- Trae's Anthropic client sends `x-api-key` and `anthropic-version`
  headers; the proxy ignores both and authenticates to the upstream
  provider with its own key, so any placeholder key works.
- Trae runs the model call from the local IDE process, so the proxy only
  needs to be reachable from the machine running Trae — `127.0.0.1` or
  the Tailscale address of the proxy host both work. Check the traffic
  log (`logs/myproxy-traffic.txt`) to see the connecting host.

## Supported request features

The `/v1/messages` endpoint accepts the full Anthropic Messages request shape and forwards the following spec fields to the upstream provider:

| Field | Notes |
|-------|-------|
| `model`, `messages`, `system`, `max_tokens`, `temperature`, `stream` | Always forwarded |
| `tools`, `tool_choice` | Forwarded; `tool_choice` is restricted to `auto` / `none` per spec |
| `top_p`, `service_tier` | Forwarded when set |
| `thinking` | Forwarded to MiniMax for the `MiniMax-M3` family (`MiniMax-M3`, `MiniMax-M3.1-Flash`); dropped for M2.x, which always thinks |
| `output_config.effort` | Not forwarded — M3.1-Flash thinking depth is not tunable through this proxy |
| `cache_control` | Preserved on content blocks, system blocks, and tool definitions |
| `source` (image / video) | Preserved on content blocks (M3 family only) |
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

Currently registered:

| Alias | Provider | Upstream model | Context |
|-------|----------|----------------|---------|
| `claude-lb-minimax-m3` | minimax | `MiniMax-M3` | 1M |
| `claude-lb-minimax-m3.1-flash` | minimax | `MiniMax-M3.1-Flash` | 1M |
| `claude-lb-minimax-m2.7` | minimax | `MiniMax-M2.7` | 200K |
| `claude-lb-minimax-m2.7-flash` | minimax | `MiniMax-M2.7-highspeed` | 200K |
| `claude-lb-deepseek-v4-pro` | deepseek | `deepseek-v4-pro` | 1M |
| `claude-lb-deepseek-flash` | deepseek | `deepseek-flash` | 1M |

The `context_length` field is informational — it's exposed via the `X-Context-Length` response header so clients can budget their requests.

## Environment Variables

| Variable | Description |
|----------|-------------|
| `MINIMAX_API_KEYS` | Comma-separated MiniMax API keys (round-robin). Provider is enabled if any key is set. |
| `DEEPSEEK_API_KEYS` | Comma-separated DeepSeek API keys (round-robin). Provider is enabled if any key is set. |
| `MYPROXY_CONFIG_DIR` | Override config directory |
| `MYPROXY_HOST` | Bind host (default `0.0.0.0`) |
| `MYPROXY_PORT` | Bind port (default `3566`) |
| `PAYLOAD_LOG_ENABLED` | Enable the request/response traffic log (default `true`) |
| `PAYLOAD_LOG_FILE` | Traffic log path (default `logs/myproxy-traffic.txt`) |
| `PAYLOAD_LOG_MAX_BYTES` | Rotate the traffic log at this size (default `52428800` = 50 MB) |
| `PAYLOAD_LOG_BACKUPS` | Rotated files to keep (default `1`) |
| `TRACE_ENABLED` | Enable the JSONL trace file (default `true`) |
| `TRACE_FILE` | Trace file path (default `logs/myproxy.jsonl`) |

## Traffic log

Every `/v1/messages` request is appended to a human-readable `.txt` file — the
request body that went to the model, and the response body that came back.

```
====================================================================================================
[2026-11-10T09:49:18.841Z] request_id=req_72381fdd5b624c1e8b2ecd6b model=claude-lb-minimax-m3 provider=minimax
  upstream_model=MiniMax-M3 stream=false status=200 latency_ms=689.123
  input_tokens=42 output_tokens=17
--- REQUEST ---
{
  "model": "MiniMax-M3",
  "max_tokens": 100,
  "messages": [{"role": "user", "content": "What is 2+2?"}]
}
--- RESPONSE ---
{
  "id": "msg_5cbd185c401b481fb9b14f49",
  "type": "message",
  "role": "assistant",
  "content": [{"type": "text", "text": "Hello!"}],
  "usage": {"input_tokens": 42, "output_tokens": 17}
}
====================================================================================================
```

Behavior:

- The **REQUEST** section is the exact body sent upstream — same model name,
  same fields, same provider-specific quirks (e.g. MiniMax drops `thinking`
  for the M2.x family).
- The **RESPONSE** section is the Anthropic-shaped message. Streaming requests
  are reassembled into the same shape as a non-streaming reply, so both modes
  read identically.
- Failed requests log an `--- ERROR ---` section instead of a response.
- Bodies are **scrubbed** — API keys, emails, credit cards, JWTs and the like
  are replaced with `[REDACTED_*]` before anything hits the disk. Requests
  rejected before reaching a provider (unknown model, missing API key) are
  logged too.
- The file rotates at `PAYLOAD_LOG_MAX_BYTES` (50 MB by default), keeping one
  previous copy as `myproxy-traffic.1.txt`.
- The log is local-only — it is never written to the trace file or exposed
  over the admin API.

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

