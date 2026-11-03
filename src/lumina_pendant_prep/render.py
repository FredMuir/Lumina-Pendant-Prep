from __future__ import annotations

from dataclasses import dataclass
from PIL import Image, ImageChops, ImageDraw


@dataclass(frozen=True)
class PendantSettings:
    crop: tuple[float, float, float, float]
    shape: str = "Rounded rectangle"
    corner_radius_mm: float = 3.0
    target_width_mm: float = 60.0
    hole_enabled: bool = True
    hole_x: float = 0.5
    hole_y: float = 0.12
    hole_diameter_mm: float = 4.0
    polygon: tuple[tuple[float, float], ...] = ()


def make_pendant(image: Image.Image, settings: PendantSettings) -> Image.Image:
    width, height = image.size
    left, top, right, bottom = settings.crop
    x0 = max(0, min(width - 1, round(left * width)))
    y0 = max(0, min(height - 1, round(top * height)))
    x1 = max(x0 + 1, min(width, round(right * width)))
    y1 = max(y0 + 1, min(height, round(bottom * height)))
    source_crop = image.convert("RGBA").crop((x0, y0, x1, y1))
    source_w, source_h = source_crop.size
    offset_x = offset_y = 0
    if settings.shape == "Circle":
        diameter = min(source_w, source_h)
        offset_x, offset_y = (source_w - diameter) // 2, (source_h - diameter) // 2
        cropped = source_crop.crop((offset_x, offset_y, offset_x + diameter, offset_y + diameter))
    else:
        cropped = source_crop
    w, h = cropped.size
    scale = w / settings.target_width_mm if settings.target_width_mm > 0 else 1.0
    supersample = 2 if max(w, h) < 6000 else 1
    mask = Image.new("L", (w * supersample, h * supersample), 0)
    draw = ImageDraw.Draw(mask)
    if settings.shape == "Circle":
        draw.ellipse((0, 0, w * supersample - 1, h * supersample - 1), fill=255)
    elif settings.shape == "Rounded rectangle":
        radius = max(0, min(min(w, h) // 2, round(settings.corner_radius_mm * scale))) * supersample
        draw.rounded_rectangle((0, 0, w * supersample - 1, h * supersample - 1), radius=radius, fill=255)
    elif settings.shape == "Polygon" and len(settings.polygon) >= 3:
        points = [
            (round((x - left) / max(right - left, 0.001) * w * supersample),
             round((y - top) / max(bottom - top, 0.001) * h * supersample))
            for x, y in settings.polygon
        ]
        draw.polygon(points, fill=255)
    else:
        draw.rectangle((0, 0, w * supersample - 1, h * supersample - 1), fill=255)
    if settings.hole_enabled:
        radius = max(1, round(settings.hole_diameter_mm * scale / 2)) * supersample
        cx = round((settings.hole_x * source_w - offset_x) * supersample)
        cy = round((settings.hole_y * source_h - offset_y) * supersample)
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=0)
    if supersample != 1:
        mask = mask.resize((w, h), Image.Resampling.LANCZOS)
    cropped.putalpha(ImageChops.multiply(cropped.getchannel("A"), mask))
    dpi = 25.4 * w / settings.target_width_mm if settings.target_width_mm > 0 else 300
    cropped.info["dpi"] = (dpi, dpi)
    return cropped

