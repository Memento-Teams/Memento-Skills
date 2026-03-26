"""Bootstrap helpers for Memento-S.

This module is referenced by the CLI and GUI entrypoints, but is missing in the
current upstream branch. We provide a minimal local implementation that:
- ensures the user config exists and is loaded
- creates the expected runtime directories
- exposes bootstrap()/bootstrap_sync() used by the app
- keeps the Feishu bridge started flag expected elsewhere
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from middleware.config import g_config

_feishu_bridge_started = False
_bootstrapped = False


def _iter_runtime_dirs() -> Iterable[Path]:
    """Yield runtime directories that should exist after bootstrap."""
    yield g_config.get_data_dir()
    yield g_config.user_config_dir

    paths = getattr(g_config, "paths", None)
    if not paths:
        return

    for maybe_path in (
        paths.workspace_dir,
        paths.skills_dir,
        paths.db_dir,
        paths.logs_dir,
        paths.venv_dir,
        paths.context_dir,
    ):
        if maybe_path:
            yield Path(maybe_path)


def bootstrap(force: bool = False):
    """Load config and create runtime directories."""
    global _bootstrapped
    if _bootstrapped and not force:
        return g_config

    g_config.load()

    for path in _iter_runtime_dirs():
        path.mkdir(parents=True, exist_ok=True)

    _bootstrapped = True
    return g_config


def bootstrap_sync(force: bool = False):
    """Synchronous bootstrap entrypoint used by CLI/GUI."""
    return bootstrap(force=force)
