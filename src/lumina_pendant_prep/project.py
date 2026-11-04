from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
import tempfile
from typing import Any


PROJECT_VERSION = 1


def source_path_for_project(project_path: str | Path, source_path: str | Path) -> str:
    if not source_path:
        return ""
    source = Path(source_path).resolve()
    try:
        return str(source.relative_to(Path(project_path).resolve().parent))
    except ValueError:
        return str(source)


def resolve_project_source(project_path: str | Path, source_path: str) -> str:
    if not source_path:
        return ""
    source = Path(source_path)
    if not source.is_absolute():
        source = Path(project_path).resolve().parent / source
    return str(source)


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
    return validate_project_data(data)


def validate_project_data(data: dict[str, Any]) -> dict[str, Any]:
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
    if "protected_regions" in data:
        regions = data["protected_regions"]
        if not (isinstance(regions, list) and len(regions) <= 100 and all(
                isinstance(r, (list, tuple)) and len(r) == 4
                and all(number(v, 0, 1) for v in r) and r[0] < r[2] and r[1] < r[3]
                for r in regions)):
            raise ValueError("Invalid protected regions")
    for key, low, high in (("target_width_mm", 1, 300), ("nozzle_mm", 0.1, 2),
                           ("min_feature_mm", 0, 5), ("color_delta", 0, 40),
                           ("corner_radius_mm", 0, 30), ("hole_diameter_mm", 0.5, 30),
                           ("hole_compensation_mm", 0, 5), ("outer_border_mm", .1, 5),
                           ("hole_border_mm", .1, 5), ("distinct_color_delta", 0, 100), ("depth_cleanup_mm", .05, 2)):
        if key in data and not number(data[key], low, high):
            raise ValueError(f"Invalid {key}")
    if "colors" in data and (type(data["colors"]) is not int or not 2 <= data["colors"] <= 128):
        raise ValueError("Invalid colors")
    for key, low, high in (("flat_min_area_px", 1, 1000000), ("heightmap_strength_percent", 0, 200), ("edge_smoothing_strength", 0, 100), ("heightmap_terrace_levels", 2, 32)):
        if key in data and (type(data[key]) is not int or not low <= data[key] <= high):
            raise ValueError(f"Invalid {key}")
    if "max_dimension" in data and (type(data["max_dimension"]) is not int or data["max_dimension"] not in (0, 2400, 4096)):
        raise ValueError("Invalid output resolution")
    for key in ("cleanup", "smooth_edges", "hole_enabled", "outer_border_enabled", "hole_border_enabled", "protect_distinct_colors", "preserve_flat_colors", "heightmap_enabled", "heightmap_dark_high", "heightmap_far_high", "heightmap_border_high", "heightmap_terraced", "depth_cleanup"):
        if key in data and type(data[key]) is not bool:
            raise ValueError(f"Invalid {key}")
    for key in ("outer_border_color", "hole_border_color"):
        if key in data and (not isinstance(data[key], str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", data[key])):
            raise ValueError(f"Invalid {key}")
    if "shape" in data and data["shape"] not in ("圆角矩形", "矩形", "圆形", "自定义多边形"):
        raise ValueError("Invalid shape")
    return data

