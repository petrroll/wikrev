from __future__ import annotations

import importlib.metadata
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

SDK_DISTRIBUTION_NAME = "github-copilot-sdk"


@dataclass(frozen=True)
class PythonEnvironment:
    python_executable: str
    copilot_sdk_version: str | None


@dataclass(frozen=True)
class RuntimeEnvironmentSnapshot:
    active: PythonEnvironment
    repo_venv: PythonEnvironment | None

    @property
    def active_uses_repo_venv(self) -> bool:
        if self.repo_venv is None:
            return False
        return _same_path(self.active.python_executable, self.repo_venv.python_executable)


def _same_path(left: str, right: str) -> bool:
    try:
        return str(Path(left).resolve()).casefold() == str(Path(right).resolve()).casefold()
    except OSError:
        return Path(left).as_posix().casefold() == Path(right).as_posix().casefold()


def _get_installed_sdk_version() -> str | None:
    try:
        return importlib.metadata.version(SDK_DISTRIBUTION_NAME)
    except importlib.metadata.PackageNotFoundError:
        return None


def _read_sdk_version_from_python(python_executable: Path) -> str | None:
    try:
        result = subprocess.run(
            [
                str(python_executable),
                "-c",
                (
                    "import importlib.metadata as m; "
                    "print(m.version('github-copilot-sdk'))"
                ),
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    version = result.stdout.strip()
    return version or None


def _get_repo_root() -> Path | None:
    cwd = Path.cwd()
    if (cwd / "pyproject.toml").exists() and (cwd / "src" / "wikrev").exists():
        return cwd
    return None


def _get_repo_venv_python(repo_root: Path) -> Path | None:
    candidates = (
        repo_root / ".venv" / "Scripts" / "python.exe",
        repo_root / ".venv" / "bin" / "python",
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def get_runtime_environment_snapshot() -> RuntimeEnvironmentSnapshot:
    repo_venv = None
    repo_root = _get_repo_root()
    if repo_root is not None:
        repo_python = _get_repo_venv_python(repo_root)
        if repo_python is not None:
            repo_venv = PythonEnvironment(
                python_executable=str(repo_python),
                copilot_sdk_version=_read_sdk_version_from_python(repo_python),
            )

    return RuntimeEnvironmentSnapshot(
        active=PythonEnvironment(
            python_executable=sys.executable,
            copilot_sdk_version=_get_installed_sdk_version(),
        ),
        repo_venv=repo_venv,
    )


def format_runtime_environment_details(
    snapshot: RuntimeEnvironmentSnapshot | None = None,
) -> str:
    snapshot = snapshot or get_runtime_environment_snapshot()
    details = [
        f"Active Python: `{snapshot.active.python_executable}`.",
        f"Active `github-copilot-sdk`: `{snapshot.active.copilot_sdk_version or 'not installed'}`.",
    ]
    if snapshot.repo_venv is not None:
        details.extend(
            [
                f"Repo `.venv` Python: `{snapshot.repo_venv.python_executable}`.",
                f"Repo `.venv` `github-copilot-sdk`: `{snapshot.repo_venv.copilot_sdk_version or 'not installed'}`.",
            ]
        )
    return " ".join(details)


def get_external_repo_venv_warning() -> str | None:
    snapshot = get_runtime_environment_snapshot()
    if snapshot.repo_venv is None or snapshot.active_uses_repo_venv:
        return None

    return (
        "Warning: WikRev is not running from this repo's `.venv`. "
        f"{format_runtime_environment_details(snapshot)} "
        "`uv sync` updates the repo environment, so start WikRev with `uv run wikrev` "
        "if you want the repo-pinned dependencies."
    )
