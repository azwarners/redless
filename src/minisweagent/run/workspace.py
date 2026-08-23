"""Create a disposable workspace for a local-model run."""

import subprocess
from pathlib import Path

import typer
import yaml
from rich.console import Console

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console(highlight=False)


@app.callback()
def main():
    """Set up a disposable workspace for mini-swe-agent-slow."""


@app.command()
def init(
    path: Path = typer.Argument(..., help="New workspace directory"),
    model: str = typer.Option(..., "--model", help="Model name accepted by the local server"),
    api_base: str = typer.Option("http://127.0.0.1:8080/v1", "--api-base", help="OpenAI-compatible server URL"),
    api_key: str = typer.Option("llama.cpp-placeholder", "--api-key", help="Server API key, if required"),
):
    """Create a new Git workspace and local-model settings file."""
    if path.exists():
        raise typer.BadParameter(f"Workspace already exists: {path}")
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    settings_path = path / ".mini-swe-agent-slow" / "llama-local.yaml"
    settings_path.parent.mkdir()
    settings_path.write_text(
        yaml.safe_dump(
            {
                "model": {
                    "model_name": model,
                    "model_kwargs": {"custom_llm_provider": "openai", "api_base": api_base, "api_key": api_key},
                }
            },
            sort_keys=False,
        )
    )
    exclude_path = path / ".git" / "info" / "exclude"
    exclude_path.write_text(exclude_path.read_text() + "\n.mini-swe-agent-slow/\n")
    console.print(f"[bold green]Workspace ready:[/bold green] {path}")
    console.print("\nNext, run:")
    console.print(
        f"cd {path}\nMSWEA_CONFIGURED=true mini-slow -c slow_local.yaml "
        "-c .mini-swe-agent-slow/llama-local.yaml -t 'Describe the task here.'"
    )


if __name__ == "__main__":
    app()
