from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterable


def ensure_sys_path(paths: Iterable[Path]) -> None:
    for path in paths:
        if not path:
            continue
        if not path.exists():
            continue
        normalized = str(path)
        if normalized not in sys.path:
            sys.path.insert(0, normalized)


def ensure_footprint_repo_on_sys_path(repo_root: Path) -> None:
    ensure_sys_path([repo_root, repo_root / "src"])


def ensure_symbol_repo_on_sys_path(repo_root: Path) -> None:
    ensure_sys_path([repo_root, repo_root / "common"])
