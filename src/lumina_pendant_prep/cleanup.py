from __future__ import annotations

from dataclasses import dataclass
import math

import cv2
import numpy as np
from PIL import Image, ImageFilter


@dataclass(frozen=True)
class CleanupSettings:
    colors: int = 32
    target_width_mm: float = 60.0
    min_feature_mm: float = 0.45
    color_delta: float = 8.0


@dataclass(frozen=True)
class CleanupResult:
    image: Image.Image
    changed_mask: Image.Image
    original_colors: int
    resulting_colors: int
    merged_islands: int
    minimum_feature_px: int
    narrow_features: int


def _lab_palette(rgb_palette: list[int], used: set[int]) -> dict[int, np.ndarray]:
    swatches = np.zeros((len(rgb_palette) // 3, 1, 3), dtype=np.float32)
    swatches[:, 0, :] = np.asarray(rgb_palette, dtype=np.float32).reshape(-1, 3) / 255.0
    lab = cv2.cvtColor(swatches, cv2.COLOR_RGB2LAB)[:, 0, :]
    return {index: lab[index] for index in used}


def simplify_image(image: Image.Image, settings: CleanupSettings) -> CleanupResult:
    """Quantize without dithering, merge adjacent similar colors, then absorb tiny islands."""
    if not 2 <= settings.colors <= 256:
        raise ValueError("Color count must be between 2 and 256")
    if settings.target_width_mm <= 0 or settings.min_feature_mm < 0:
        raise ValueError("Print width must be positive and feature width non-negative")
    rgba = image.convert("RGBA")
    alpha = np.asarray(rgba.getchannel("A"), dtype=np.uint8)
    opaque = alpha > 0
    rgb_pixels = np.asarray(rgba.convert("RGB"), dtype=np.uint8).copy()
    if np.any(opaque) and np.any(~opaque):
        # Transparent pixels must not consume palette entries or influence the
        # frequency of visible colors. Train on visible pixels only.
        visible = Image.fromarray(rgb_pixels[opaque].reshape(-1, 1, 3), "RGB")
        trained = visible.quantize(colors=settings.colors, method=Image.Quantize.MEDIANCUT,
                                   dither=Image.Dither.NONE)
        quantized = trained
        labels = np.zeros(alpha.shape, dtype=np.uint8)
        labels[opaque] = np.asarray(trained, dtype=np.uint8).reshape(-1)
    else:
        rgb = Image.fromarray(rgb_pixels, "RGB")
        quantized = rgb.quantize(colors=settings.colors, method=Image.Quantize.MEDIANCUT,
                                 dither=Image.Dither.NONE)
        labels = np.asarray(quantized, dtype=np.uint8).copy()
    used = set(int(value) for value in np.unique(labels[opaque])) if np.any(opaque) else set()
    original_colors = len(used)
    palette = quantized.getpalette() or []
    labs = _lab_palette(palette, used) if used else {}

    # Merge palette entries only when they share an edge and are perceptually close.
    links: dict[tuple[int, int], int] = {}
    edges = (
        (labels[:, :-1], labels[:, 1:], opaque[:, :-1] & opaque[:, 1:]),
        (labels[:-1, :], labels[1:, :], opaque[:-1, :] & opaque[1:, :]),
    )
    for a, b, edge_mask in edges:
        first, second = a[edge_mask].astype(np.uint16), b[edge_mask].astype(np.uint16)
        if first.size:
            low, high = np.minimum(first, second), np.maximum(first, second)
            counts = np.bincount((low * 256 + high).astype(np.int32), minlength=65536)
            for index in np.flatnonzero(counts):
                left, right = divmod(int(index), 256)
                if left != right:
                    links[(left, right)] = links.get((left, right), 0) + int(counts[index])
    parent = {color: color for color in used}
    color_counts = np.bincount(labels[opaque], minlength=256)

    def root(value: int) -> int:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    for (left, right), _contact in sorted(links.items()):
        left_root, right_root = root(left), root(right)
        if left_root == right_root or left_root not in labs or right_root not in labs:
            continue
        delta = float(np.linalg.norm(labs[left_root] - labs[right_root]))
        if delta <= settings.color_delta:
            # Keep the dominant palette member; use the lower index for exact ties.
            if (color_counts[left_root], -left_root) >= (color_counts[right_root], -right_root):
                parent[right_root] = left_root
            else:
                parent[left_root] = right_root
    remap = np.arange(256, dtype=np.uint8)
    for color in used:
        remap[color] = root(color)
    labels[opaque] = remap[labels[opaque]]

    width, height = image.size
    minimum_feature_px = max(1, math.ceil(width / settings.target_width_mm * settings.min_feature_mm))
    minimum_area = minimum_feature_px * minimum_feature_px
    merged_islands = 0
    narrow_features = 0
    if minimum_area > 1:
        # Update labels after each merge so adjacent tiny components cannot
        # exchange colors simultaneously and remain fragmented.
        kernel = np.asarray([[0, 1, 0], [1, 1, 1], [0, 1, 0]], dtype=np.uint8)
        for color in sorted(int(v) for v in np.unique(labels[opaque])):
            mask = ((labels == color) & opaque).astype(np.uint8)
            count, components, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=4)
            for component_id in range(1, count):
                area = int(stats[component_id, cv2.CC_STAT_AREA])
                x = int(stats[component_id, cv2.CC_STAT_LEFT])
                y = int(stats[component_id, cv2.CC_STAT_TOP])
                box_w = int(stats[component_id, cv2.CC_STAT_WIDTH])
                box_h = int(stats[component_id, cv2.CC_STAT_HEIGHT])
                x0, y0 = max(0, x - 1), max(0, y - 1)
                x1, y1 = min(width, x + box_w + 1), min(height, y + box_h + 1)
                component = components[y0:y1, x0:x1] == component_id
                if minimum_area <= area < minimum_area * 16:
                    distance = cv2.distanceTransform(component.astype(np.uint8), cv2.DIST_L2, 3)
                    if float(distance.max()) * 2 < minimum_feature_px:
                        narrow_features += 1
                if area >= minimum_area:
                    continue
                local_opaque = opaque[y0:y1, x0:x1]
                boundary = (cv2.dilate(component.astype(np.uint8), kernel, iterations=1) > 0) & ~component & local_opaque
                neighbor_labels, contact = np.unique(labels[y0:y1, x0:x1][boundary], return_counts=True)
                candidates = [(int(n), int(c)) for n, c in zip(neighbor_labels, contact) if int(n) != color]
                if not candidates:
                    continue
                source_lab = labs.get(color, np.zeros(3, dtype=np.float32))
                chosen = min(
                    candidates,
                    key=lambda pair: (
                        float(np.linalg.norm(source_lab - labs.get(pair[0], source_lab)))
                        / math.sqrt(pair[1]),
                        -pair[1],
                        pair[0],
                    ),
                )[0]
                labels[y0:y1, x0:x1][component] = chosen
                merged_islands += 1

    output_rgb = np.zeros((height, width, 3), dtype=np.uint8)
    for color in np.unique(labels[opaque]):
        color = int(color)
        output_rgb[(labels == color) & opaque] = palette[color * 3:color * 3 + 3]
    output = Image.fromarray(np.dstack((output_rgb, alpha)), "RGBA")
    original_rgb = rgb_pixels.astype(np.int16)
    changed = np.any(np.abs(output_rgb.astype(np.int16) - original_rgb) > 0, axis=2) & opaque
    changed_mask = Image.fromarray((changed.astype(np.uint8) * 255), "L").filter(ImageFilter.MaxFilter(3))
    return CleanupResult(
        image=output,
        changed_mask=changed_mask,
        original_colors=original_colors,
        resulting_colors=len(np.unique(labels[opaque])) if np.any(opaque) else 0,
        merged_islands=merged_islands,
        minimum_feature_px=minimum_feature_px,
        narrow_features=narrow_features,
    )

