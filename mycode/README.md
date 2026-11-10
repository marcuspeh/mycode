# mycode

Interactive CLI wrapper that launches the **Claude Code CLI** (the `claude`
command-line tool, not the Claude IDE extension) with a custom LLM backend
via myproxy.

## Usage

```bash
# Interactive picker — fetches the model list from myproxy, then asks
# which model to use for opus / sonnet / haiku
mycode

# Extra args are forwarded to claude code
mycode --debug --some-flag=value
```

The picker runs three times, once per slot, and each selection is
exported as the matching `ANTHROPIC_DEFAULT_*_MODEL` variable.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_BASE_URL` | `http://127.0.0.1:3566` | myproxy server URL |
| `CLAUDE_BIN` | `claude` | Path to claude binary |

The proxy URL is currently fixed to `http://127.0.0.1:3566`.

## Install

```bash
uv sync
uv run mycode
```

The interactive picker requires `questionary` (installed automatically with the package).