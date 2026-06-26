# mycode

Interactive CLI wrapper that launches the **Claude Code CLI** (the `claude`
command-line tool, not the Claude IDE extension) with a custom LLM backend
via myproxy.

## Usage

```bash
# Interactive: fetch available models and pick with arrow keys
mycode

# Or pass --model directly to skip the picker
mycode --model minimax-3

# Extra args are forwarded to claude code
mycode --model minimax-3 --debug --some-flag=value

# Custom myproxy server
mycode --base-url http://192.168.1.100:3566
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_BASE_URL` | `http://genmachine:3566` | myproxy server URL |
| `ANTHROPIC_MODEL` | `claude-{model}` | Model name sent to proxy |
| `MYCODE_MACHINE` | hostname | Machine identifier |
| `CLAUDE_BIN` | `claude` | Path to claude binary |

## Install

```bash
pip install -e .
```

The interactive picker requires `questionary` (installed automatically with the package).