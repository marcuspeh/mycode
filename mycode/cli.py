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
DEFAULT_BASE_URL = "http://127.0.0.1:3566"
DEFAULT_MACHINE = os.environ.get("MYCODE_MACHINE") or os.uname().nodename


def _fetch_models(base_url: str) -> list[tuple[str, dict[str, Any]]]:
    """Fetch model list from myproxy. Returns list of (alias, spec) tuples."""
    try:
        response = httpx.get(f"{base_url}/admin/models", timeout=5.0)
        response.raise_for_status()
        data = response.json()
        return list(data.get("models", {}).items())
    except Exception as e:
        typer.echo(f"error: failed to fetch models from {base_url}: {e}", err=True)
        raise typer.Exit(code=1)


def _select_model_interactive(
    models: list[tuple[str, dict[str, Any]]],
    prompt: str = "Select a model:",
) -> tuple[str, dict[str, Any]]:
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
            title=f"{spec.get('model', '?')}",
            value=(alias, spec),
        )
        for alias, spec in models
    ]

    selected = questionary.select(
        prompt,
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


@app.callback()
def main() -> None:
    """Launch Claude Code pointed at myproxy."""
    models = _fetch_models(DEFAULT_BASE_URL)
    if not models:
        typer.echo("error: no models configured on the proxy", err=True)
        raise typer.Exit(code=1)
    name, chosen = _select_model_interactive(models, prompt="Select main model:")
    actual = chosen.get("model", "unknown")
    typer.echo(f"main model: {name} ({actual})")

    fast_name, fast_chosen = _select_model_interactive(
        models, prompt="Select small/fast model:"
    )
    fast_actual = fast_chosen.get("model", "unknown")
    typer.echo(f"small/fast model: {fast_name} ({fast_actual})")

    env = os.environ.copy()
    env["ANTHROPIC_BASE_URL"] = DEFAULT_BASE_URL
    env["ANTHROPIC_MODEL"] = name
    env["ANTHROPIC_SMALL_FAST_MODEL"] = fast_name
    env["MYCODE_MACHINE"] = DEFAULT_MACHINE
    env.setdefault("ANTHROPIC_AUTH_TOKEN", "sk-dummy")

    claude_bin = _find_claude()
    extra_args = sys.argv[1:]

    typer.echo(
        f"mycode: launching claude "
        f"(base_url={DEFAULT_BASE_URL}, model={name}, small_fast={fast_name}, extra_args={extra_args})"
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