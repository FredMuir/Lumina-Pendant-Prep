"""Local monocular relative-depth inference and Lumina image alignment."""
from pathlib import Path
import os

import cv2
import numpy as np
from PIL import Image, ImageChops, ImageOps

from .render import crop_pixel_box


MODEL_NAME = "depth-anything-v2-small-int8.onnx"


def model_path() -> Path:
    bundled = Path(__file__).parent / "assets" / "models" / MODEL_NAME
    if bundled.is_file():
        return bundled
    root = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share"))
    return root / "LuminaPendantPrep" / "models" / MODEL_NAME


def estimate_relative_depth(image: Image.Image, cancelled=None) -> np.ndarray:
    """Estimate inverse depth in memory; larger values mean closer to camera."""
    import onnxruntime as ort

    path = model_path()
    if not path.is_file():
        raise FileNotFoundError("深度模型未安装；请重新安装软件或运行 scripts/fetch_depth_model.ps1。")
    if cancelled and cancelled():
        raise InterruptedError("深度估计已取消")
    rgba = image.convert("RGBA")
    background = Image.new("RGB", rgba.size, (127, 127, 127))
    background.paste(rgba, mask=rgba.getchannel("A"))
    rgb = np.asarray(background, dtype=np.uint8)
    h, w = rgb.shape[:2]
    scale = min(518 / min(w, h), 1036 / max(w, h))
    target_w = max(14, round(w * scale / 14) * 14)
    target_h = max(14, round(h * scale / 14) * 14)
    resized = cv2.resize(rgb, (target_w, target_h), interpolation=cv2.INTER_CUBIC).astype(np.float32) / 255.0
    resized = (resized - np.array([.485, .456, .406], dtype=np.float32)) / np.array([.229, .224, .225], dtype=np.float32)
    tensor = np.ascontiguousarray(resized.transpose(2, 0, 1)[None])
    options = ort.SessionOptions()
    options.intra_op_num_threads = min(4, os.cpu_count() or 1)
    options.inter_op_num_threads = 1
    session = ort.InferenceSession(str(path), sess_options=options, providers=["CPUExecutionProvider"])
    output = session.run(None, {session.get_inputs()[0].name: tensor})[0]
    if cancelled and cancelled():
        raise InterruptedError("深度估计已取消")
    depth = np.asarray(output, dtype=np.float32).squeeze()
    if depth.ndim != 2 or not np.isfinite(depth).all():
        raise ValueError("模型返回的深度数据无效。")
    return cv2.resize(depth, (w, h), interpolation=cv2.INTER_CUBIC)


def align_depth(depth: np.ndarray, color: Image.Image, settings) -> Image.Image:
    """Crop/pad depth to the processed color output and normalize visible content."""
    h, w = depth.shape
    x0, y0, x1, y1 = crop_pixel_box((w, h), settings.crop)
    cropped = depth[y0:y1, x0:x1]
    if settings.shape == "Circle":
        side = min(cropped.shape)
        dy, dx = (cropped.shape[0] - side) // 2, (cropped.shape[1] - side) // 2
        cropped = cropped[dy:dy + side, dx:dx + side]
    oh, ow = color.height, color.width
    pad_x, pad_y = (ow - cropped.shape[1]) // 2, (oh - cropped.shape[0]) // 2
    if pad_x < 0 or pad_y < 0 or ow - cropped.shape[1] != 2 * pad_x or oh - cropped.shape[0] != 2 * pad_y:
        raise ValueError("深度图与彩色输出的尺寸不一致，请重新处理照片。")
    alpha = np.asarray(color.convert("RGBA").getchannel("A"), dtype=np.uint8)
    content_alpha = alpha[pad_y:pad_y + cropped.shape[0], pad_x:pad_x + cropped.shape[1]]
    valid = (content_alpha > 0) & np.isfinite(cropped)
    if not np.any(valid):
        raise ValueError("图片没有可用于高度图的不透明区域。")
    low, high = np.percentile(cropped[valid], [2, 98])
    normalized = np.zeros(cropped.shape, dtype=np.uint8)
    if high > low + 1e-8:
        normalized = np.rint(np.clip((cropped - low) / (high - low), 0, 1) * 255).astype(np.uint8)
    normalized[~valid] = 0
    result = Image.new("L", color.size, 0)
    result.paste(Image.fromarray(normalized), (pad_x, pad_y))
    return result


def depth_heightmap(depth: Image.Image, color: Image.Image, far_high: bool, strength: float) -> Image.Image:
    if depth.size != color.size or not 0 <= strength <= 2:
        raise ValueError("高度图尺寸或强度无效。")
    image = depth.convert("L")
    if far_high:
        image = ImageOps.invert(image)
    if strength != 1:
        image = image.point(lambda value: min(255, round(value * strength)))
    result = ImageChops.multiply(image, color.convert("RGBA").getchannel("A"))
    result.info.clear()
    result.info["dpi"] = color.info.get("dpi", (300, 300))
    return result
