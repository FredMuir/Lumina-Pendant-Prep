from __future__ import annotations

import json
from pathlib import Path
from typing import Any


PROJECT_VERSION = 1


def save_project(path: str | Path, data: dict[str, Any]) -> None:
    payload = {"format": "lumina-pendant-prep", "version": PROJECT_VERSION, **data}
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_project(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("format") != "lumina-pendant-prep":
        raise ValueError("This is not a Lumina Pendant Prep project file")
    if data.get("version") != PROJECT_VERSION:
        raise ValueError(f"Unsupported project version: {data.get('version')}")
    return data

