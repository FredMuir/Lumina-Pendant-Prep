"""User-named settings, stored independently of image files and installations."""
from pathlib import Path
import json
import os
import tempfile

from .project import validate_project_data


def preset_path():
    root = Path(os.environ.get("APPDATA") or (Path.home() / ".config"))
    return root / "LuminaPendantPrep" / "presets.json"


def validate_presets(data):
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("presets"), dict):
        raise ValueError("预设文件格式不正确。")
    presets = data["presets"]
    if len(presets) > 200:
        raise ValueError("预设数量超过上限。")
    for name, settings in presets.items():
        if not isinstance(name, str) or not name.strip() or len(name) > 80 or not isinstance(settings, dict):
            raise ValueError("预设名称或内容无效。")
        if any(key in settings for key in ("source_path", "format", "version")):
            raise ValueError("预设包含不允许的项目字段。")
        validate_project_data({"format": "lumina-pendant-prep", "version": 1, **settings})
        if settings.get("nozzle_mm", .4) not in (.2, .4, .6):
            raise ValueError("预设中的喷嘴直径无效。")
    return presets


def load_presets(path=None):
    path = Path(path) if path is not None else preset_path()
    if not path.exists():
        return {}
    return validate_presets(json.loads(path.read_text(encoding="utf-8")))


def write_presets(presets, path=None):
    path = Path(path) if path is not None else preset_path()
    validate_presets({"version": 1, "presets": presets})
    serialized = json.dumps({"version": 1, "presets": presets}, ensure_ascii=False, indent=2, allow_nan=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".presets-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
