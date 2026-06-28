"""CLI entrypoint for myproxy."""
from __future__ import annotations

import uvicorn

import typer

app = typer.Typer(help="myproxy - Anthropic-compatible proxy server")


@app.command()
def main(
    host: str = "0.0.0.0",
    port: int = 3566,
    reload: bool = False,
) -> None:
    """Run the myproxy server."""
    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        reload=reload,
    )


if __name__ == "__main__":
    app()
