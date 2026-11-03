from __future__ import annotations

from dataclasses import dataclass
import math
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


def hole_has_clearance(image_size: tuple[int, int], settings: PendantSettings,
                       minimum_wall_mm: float = 1.2) -> bool:
    """Return whether the entire round hole plus a material wall fits inside the outline."""
    image_width, image_height = image_size
    left, top, right, bottom = settings.crop
    x0 = max(0, min(image_width - 1, round(left * image_width)))
    y0 = max(0, min(image_height - 1, round(top * image_height)))
    x1 = max(x0 + 1, min(image_width, round(right * image_width)))
    y1 = max(y0 + 1, min(image_height, round(bottom * image_height)))
    source_w, source_h = x1 - x0, y1 - y0
    offset_x = offset_y = 0
    if settings.shape == "Circle":
        side = min(source_w, source_h)
        offset_x, offset_y = (source_w - side) / 2, (source_h - side) / 2
        width = height = side
    else:
        width, height = source_w, source_h
    if width <= 0 or settings.target_width_mm <= 0:
        return False
    scale = width / settings.target_width_mm
    center_x = settings.hole_x * source_w - offset_x
    center_y = settings.hole_y * source_h - offset_y
    required_margin = (settings.hole_diameter_mm / 2 + minimum_wall_mm) * scale

    if settings.shape == "Circle":
        available = width / 2 - math.hypot(center_x - width / 2, center_y - height / 2)
    elif settings.shape == "Polygon" and len(settings.polygon) >= 3:
        vertices = [(x * image_width - x0, y * image_height - y0)
                    for x, y in settings.polygon]
        inside = False
        min_distance = float("inf")
        for i, (ax, ay) in enumerate(vertices):
            bx, by = vertices[(i + 1) % len(vertices)]
            if (ay > center_y) != (by > center_y):
                crossing_x = (bx - ax) * (center_y - ay) / (by - ay) + ax
                if center_x < crossing_x:
                    inside = not inside
            dx, dy = bx - ax, by - ay
            length2 = dx * dx + dy * dy
            t = 0 if length2 == 0 else max(0, min(1, ((center_x - ax) * dx + (center_y - ay) * dy) / length2))
            min_distance = min(min_distance, math.hypot(center_x - (ax + t * dx), center_y - (ay + t * dy)))
        polygon_margin = min_distance if inside else -min_distance
        # The polygon is clipped by the cropped image, so both boundaries
        # constrain the material surrounding a hole.
        available = min(polygon_margin, center_x, center_y,
                        width - center_x, height - center_y)
    elif settings.shape == "Rounded rectangle":
        radius = min(min(width, height) / 2, settings.corner_radius_mm * scale)
        if center_x < radius and center_y < radius:
            available = radius - math.hypot(center_x - radius, center_y - radius)
        elif center_x > width - radius and center_y < radius:
            available = radius - math.hypot(center_x - (width - radius), center_y - radius)
        elif center_x < radius and center_y > height - radius:
            available = radius - math.hypot(center_x - radius, center_y - (height - radius))
        elif center_x > width - radius and center_y > height - radius:
            available = radius - math.hypot(center_x - (width - radius), center_y - (height - radius))
        else:
            available = min(center_x, center_y, width - center_x, height - center_y)
    else:
        available = min(center_x, center_y, width - center_x, height - center_y)
    return available >= required_margin


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
            (round((x * width - x0) * supersample),
             round((y * height - y0) * supersample))
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

