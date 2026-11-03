from __future__ import annotations

import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any


PROJECT_VERSION = 1


def save_project(path: str | Path, data: dict[str, Any]) -> None:
    payload = {"format": "lumina-pendant-prep", "version": PROJECT_VERSION, **data}
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    destination = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent,
                                         prefix=f".{destination.name}.", suffix=".tmp", delete=False) as file:
            temporary = Path(file.name)
            file.write(serialized)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_project(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Project must contain an object")
    if data.get("format") != "lumina-pendant-prep":
        raise ValueError("This is not a Lumina Pendant Prep project file")
    if data.get("version") != PROJECT_VERSION:
        raise ValueError(f"Unsupported project version: {data.get('version')}")
    if not isinstance(data.get("source_path", ""), str):
        raise ValueError("Invalid source path")
    def number(value: Any, low: float, high: float) -> bool:
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and low <= value <= high)
    def point(value: Any) -> bool:
        return isinstance(value, (list, tuple)) and len(value) == 2 and all(number(v, 0, 1) for v in value)
    if "crop" in data:
        crop = data["crop"]
        if not (isinstance(crop, (list, tuple)) and len(crop) == 4
                and all(number(v, 0, 1) for v in crop)
                and crop[0] < crop[2] and crop[1] < crop[3]):
            raise ValueError("Invalid crop")
    if "hole" in data and not point(data["hole"]):
        raise ValueError("Invalid hole position")
    if "polygon" in data and not (isinstance(data["polygon"], list) and all(point(p) for p in data["polygon"])):
        raise ValueError("Invalid polygon")
    for key, low, high in (("target_width_mm", 1, 300), ("nozzle_mm", 0.1, 2),
                           ("min_feature_mm", 0, 5), ("color_delta", 0, 40),
                           ("corner_radius_mm", 0, 30), ("hole_diameter_mm", 0.5, 30)):
        if key in data and not number(data[key], low, high):
            raise ValueError(f"Invalid {key}")
    if "colors" in data and (type(data["colors"]) is not int or not 2 <= data["colors"] <= 128):
        raise ValueError("Invalid colors")
    for key in ("cleanup", "hole_enabled"):
        if key in data and type(data[key]) is not bool:
            raise ValueError(f"Invalid {key}")
    if "shape" in data and data["shape"] not in ("圆角矩形", "矩形", "圆形", "自定义多边形"):
        raise ValueError("Invalid shape")
    return data

