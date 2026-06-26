"""mycode — interactive CLI wrapper that launches the `claude` CLI (Claude Code).

This launches the Claude Code command-line tool (not the Claude IDE extension)
with ANTHROPIC_BASE_URL pointed at myproxy, so any model from the registry
can be used as a drop-in replacement.

Usage:
    mycode                       # Fetch models, pick interactively
    mycode --model minimax-3     # Skip picker, use given model
    mycode --model minimax-3 --some-claude-flag=foo   # Extra flags forwarded
"""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Any

import httpx
import typer

app = typer.Typer(
    name="mycode",
    help="Launch Claude Code with a custom LLM backend via myproxy.",
    add_completion=False,
    invoke_without_command=True,
)

# Defaults
DEFAULT_BASE_URL = "http://genmachine:8000"
DEFAULT_MACHINE = os.environ.get("MYCODE_MACHINE") or os.uname().nodename


def _fetch_models(base_url: str) -> list[dict[str, Any]]:
    """Fetch model list from myproxy."""
    try:
        response = httpx.get(f"{base_url}/admin/models", timeout=5.0)
        response.raise_for_status()
        data = response.json()
        return list(data.get("models", {}).values())
    except Exception as e:
        typer.echo(f"error: failed to fetch models from {base_url}: {e}", err=True)
        raise typer.Exit(code=1)


def _select_model_interactive(models: list[dict[str, Any]]) -> dict[str, Any]:
    """Prompt user to pick a model with arrow keys."""
    try:
        import questionary
        from questionary import Choice
    except ImportError:
        typer.echo(
            "error: questionary is required for interactive mode. "
            "Install with: pip install questionary",
            err=True,
        )
        raise typer.Exit(code=1)

    choices = [
        Choice(
            title=f"{m.get('provider', '?')}: {m.get('model', '?')}",
            value=m,
        )
        for m in models
    ]

    selected = questionary.select(
        "Select a model:",
        choices=choices,
    ).ask()

    if selected is None:
        typer.echo("cancelled")
        raise typer.Exit(code=0)

    return selected


def _find_claude() -> str:
    """Find the claude binary."""
    explicit = os.environ.get("CLAUDE_BIN")
    if explicit:
        return explicit

    import shutil
    found = shutil.which("claude")
    if found:
        return found

    for candidate in (
        "/usr/local/bin/claude",
        "/opt/homebrew/bin/claude",
        str(os.path.expanduser("~/.local/bin/claude")),
    ):
        if os.path.isfile(candidate):
            return candidate

    return "claude"


def _collect_extra_args(model: str) -> list[str]:
    """Collect any extra args from sys.argv that aren't ours.

    Strategy: find `--model` and the value after it (if any), exclude those,
    pass everything else through.
    """
    args = sys.argv[1:]
    extra: list[str] = []
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg == "--model":
            skip_next = True
            continue
        if arg.startswith("--model="):
            continue
        # Skip our own base-url/machine flags
        if arg == "--base-url":
            skip_next = True
            continue
        if arg.startswith("--base-url="):
            continue
        if arg == "--machine":
            skip_next = True
            continue
        if arg.startswith("--machine="):
            continue
        extra.append(arg)
    return extra


@app.callback()
def main(
    model: str | None = typer.Option(
        None,
        "--model",
        "-m",
        help="Model name. If omitted, shows an interactive picker.",
    ),
    base_url: str = typer.Option(
        DEFAULT_BASE_URL,
        "--base-url",
        help="Base URL of the myproxy server.",
    ),
    machine: str = typer.Option(
        DEFAULT_MACHINE,
        "--machine",
        help="Machine identifier for logging.",
    ),
) -> None:
    """Launch Claude Code pointed at myproxy."""
    if model is None:
        models = _fetch_models(base_url)
        if not models:
            typer.echo("error: no models configured on the proxy", err=True)
            raise typer.Exit(code=1)
        chosen = _select_model_interactive(models)
        provider = chosen.get("provider", "unknown")
        model = chosen.get("model", "unknown")
        typer.echo(f"selected: {provider}:{model}")

    env = os.environ.copy()
    env["ANTHROPIC_BASE_URL"] = base_url
    env["ANTHROPIC_MODEL"] = f"claude-{model}"
    env["MYCODE_MACHINE"] = machine

    claude_bin = _find_claude()
    extra_args = _collect_extra_args(model or "")

    typer.echo(
        f"mycode: launching claude "
        f"(base_url={base_url}, model=claude-{model}, extra_args={extra_args})"
    )

    try:
        process = subprocess.run(
            [claude_bin, *extra_args],
            env=env,
        )
        sys.exit(process.returncode)
    except FileNotFoundError:
        typer.echo("error: could not find 'claude' binary", err=True)
        raise typer.Exit(code=1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    app()