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


def align_depth(depth: np.ndarray, color: Image.Image, settings, border_mask=None) -> Image.Image:
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
    if border_mask is not None:
        if border_mask.size != color.size:
            raise ValueError("边框蒙版尺寸不一致。")
        borders = np.asarray(border_mask, dtype=np.uint8)
        valid &= borders[pad_y:pad_y + cropped.shape[0], pad_x:pad_x + cropped.shape[1]] == 0
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


def clean_depth_islands(image: Image.Image, valid: np.ndarray, minimum_area: int) -> Image.Image:
    """Merge small equal-height components into the closest adjacent height."""
    if minimum_area <= 1:
        return image
    heights = np.asarray(image, dtype=np.uint8).copy()
    height, width = heights.shape
    for value in np.unique(heights[valid]):
        selected = ((heights == value) & valid).astype(np.uint8)
        count, labels, stats, _ = cv2.connectedComponentsWithStats(selected, connectivity=8)
        for component in range(1, count):
            x, y, w, h, area = stats[component]
            if area >= minimum_area:
                continue
            x0, y0, x1, y1 = max(0, x - 1), max(0, y - 1), min(width, x + w + 1), min(height, y + h + 1)
            region = (labels[y0:y1, x0:x1] == component) & (heights[y0:y1, x0:x1] == value)
            if not np.any(region):
                continue
            boundary = (cv2.dilate(region.astype(np.uint8), np.ones((3, 3), np.uint8)) > 0) & ~region
            boundary &= valid[y0:y1, x0:x1]
            neighbors = heights[y0:y1, x0:x1][boundary]
            neighbors = neighbors[neighbors != value]
            if neighbors.size == 0:
                continue
            choices, counts = np.unique(neighbors, return_counts=True)
            chosen = min(zip(choices.tolist(), counts.tolist()), key=lambda pair: (abs(pair[0] - int(value)), -pair[1], pair[0]))[0]
            heights[y0:y1, x0:x1][region] = chosen
    return Image.fromarray(heights, "L")


def depth_heightmap(depth: Image.Image, color: Image.Image, far_high: bool, strength: float,
                    border_mask=None, border_high=False, terrace_levels=None,
                    cleanup_mm=0) -> Image.Image:
    if depth.size != color.size or not 0 <= strength <= 2:
        raise ValueError("高度图尺寸或强度无效。")
    image = depth.convert("L")
    if far_high:
        image = ImageOps.invert(image)
    if strength != 1:
        image = image.point(lambda value: min(255, round(value * strength)))
    if terrace_levels is not None:
        if type(terrace_levels) is not int or not 2 <= terrace_levels <= 32:
            raise ValueError("梯田高度层数必须为 2 到 32。")
        # Map intensity to evenly spaced plateaus, including exact black/white.
        levels = terrace_levels - 1
        image = image.point(lambda value: round(round(value * levels / 255) * 255 / levels))
    if not 0 <= cleanup_mm <= 2:
        raise ValueError("高度图净化尺寸无效。")
    if cleanup_mm:
        alpha = np.asarray(color.convert("RGBA").getchannel("A"), dtype=np.uint8)
        valid = alpha == 255
        if border_mask is not None:
            if border_mask.size != color.size:
                raise ValueError("边框蒙版尺寸不一致。")
            valid &= np.asarray(border_mask, dtype=np.uint8) == 0
        dpi = color.info.get("dpi", (300, 300))[0]
        feature_px = max(1, round(cleanup_mm * dpi / 25.4))
        if terrace_levels is not None:
            image = clean_depth_islands(image, valid, feature_px * feature_px)
        elif feature_px > 1:
            # Median filtering removes isolated specks without blending slopes
            # across excluded boundaries, transparent holes or border pixels.
            kernel = min(7, feature_px if feature_px % 2 else feature_px + 1)
            valid_interior = cv2.erode(valid.astype(np.uint8), np.ones((kernel, kernel), np.uint8)) > 0
            values = np.asarray(image, dtype=np.uint8).copy()
            filtered = cv2.medianBlur(values, kernel)
            values[valid_interior] = filtered[valid_interior]
            image = Image.fromarray(values, "L")
    result = ImageChops.multiply(image, color.convert("RGBA").getchannel("A"))
    if border_mask is not None:
        if border_mask.size != color.size:
            raise ValueError("边框蒙版尺寸不一致。")
        # Border height is absolute: it is not affected by depth direction or strength.
        mask = ImageChops.multiply(border_mask.point(lambda value: 255 if value else 0),
                                   color.convert("RGBA").getchannel("A").point(lambda value: 255 if value else 0))
        result.paste(255 if border_high else 0, mask=mask)
    result.info.clear()
    result.info["dpi"] = color.info.get("dpi", (300, 300))
    return result
