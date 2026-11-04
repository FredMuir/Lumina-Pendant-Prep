from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Callable

import cv2
import numpy as np
from PIL import Image


class ProcessingCancelled(Exception):
    """Raised when an obsolete preview is cancelled by the caller."""


def _check_cancel(cancelled: Callable[[], bool] | None) -> None:
    if cancelled is not None and cancelled():
        raise ProcessingCancelled()


@dataclass(frozen=True)
class CleanupSettings:
    colors: int = 96
    target_width_mm: float = 60.0
    min_feature_mm: float = 0.45
    color_delta: float = 2.0
    protect_distinct_colors: bool = True
    preserve_flat_colors: bool | None = None
    flat_min_area_px: int = 16
    distinct_color_delta: float = 12.0
    smooth_edges: bool = False


@dataclass(frozen=True)
class CleanupResult:
    image: Image.Image
    changed_mask: Image.Image
    quantized_mask: Image.Image
    merged_mask: Image.Image
    visible_pixels: int
    quantized_pixels: int
    merged_pixels: int
    original_colors: int
    resulting_colors: int
    merged_islands: int
    protected_islands: int
    preserved_flat_colors: int
    minimum_feature_px: int
    narrow_features: int


def _lab_palette(rgb_palette: list[int], used: set[int]) -> dict[int, np.ndarray]:
    swatches = np.zeros((len(rgb_palette) // 3, 1, 3), dtype=np.float32)
    swatches[:, 0, :] = np.asarray(rgb_palette, dtype=np.float32).reshape(-1, 3) / 255.0
    lab = cv2.cvtColor(swatches, cv2.COLOR_RGB2LAB)[:, 0, :]
    return {index: lab[index] for index in used}


def _lab_distances(labs: dict[int, np.ndarray]) -> np.ndarray:
    """Cache palette distances; each color pair can occur at many boundaries."""
    values = np.zeros((256, 3), dtype=np.float32)
    for color, lab in labs.items():
        values[color] = lab
    return np.linalg.norm(values[:, None, :] - values[None, :, :], axis=2)


def _flat_color_mask(rgb: np.ndarray, visible: np.ndarray, minimum_area: int,
                     cancelled: Callable[[], bool] | None = None) -> tuple[np.ndarray, set[tuple[int, int, int]]]:
    """Identify contiguous, exactly uniform areas before quantization.

    Only common colors with adjacent equal pixels are considered. The cap
    bounds the work on highly compressed photos with many repeated colors.
    """
    packed = (rgb[:, :, 0].astype(np.uint32) << 16) | (rgb[:, :, 1].astype(np.uint32) << 8) | rgb[:, :, 2].astype(np.uint32)
    horizontal = visible[:, :-1] & visible[:, 1:] & (packed[:, :-1] == packed[:, 1:])
    vertical = visible[:-1, :] & visible[1:, :] & (packed[:-1, :] == packed[1:, :])
    candidates, counts = np.unique(np.concatenate((packed[:, :-1][horizontal],
                                                    packed[:-1, :][vertical])), return_counts=True)
    threshold = max(1, minimum_area)
    eligible = np.flatnonzero(counts >= threshold - 1)
    eligible = eligible[np.argsort(-counts[eligible], kind="stable")[:64]]
    protected = np.zeros(visible.shape, dtype=bool)
    colors = set()
    for index in eligible:
        _check_cancel(cancelled)
        matching = ((packed == candidates[index]) & visible).astype(np.uint8)
        count, components, stats, _ = cv2.connectedComponentsWithStats(matching, connectivity=4)
        keep = np.zeros(count, dtype=bool)
        keep[1:] = stats[1:, cv2.CC_STAT_AREA] >= threshold
        if np.any(keep):
            protected |= keep[components]
            value = int(candidates[index])
            colors.add(((value >> 16) & 255, (value >> 8) & 255, value & 255))
    return protected, colors


def _merge_adjacent_regions(labels: np.ndarray, visible: np.ndarray,
                            labs: dict[int, np.ndarray], threshold: float,
                            distances: np.ndarray | None = None,
                            cancelled: Callable[[], bool] | None = None) -> None:
    """Merge touching components, never all occurrences of a palette color."""
    if threshold <= 0 or not labs:
        return
    if distances is None:
        distances = _lab_distances(labs)
    regions = np.zeros(labels.shape, dtype=np.int32)
    region_colors = [0]
    region_sizes = [0]
    for color in sorted(labs):
        _check_cancel(cancelled)
        mask = ((labels == color) & visible).astype(np.uint8)
        count, components, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=4)
        offset = len(region_colors) - 1
        selected = components > 0
        regions[selected] = components[selected] + offset
        region_colors.extend([color] * (count - 1))
        region_sizes.extend(stats[1:, cv2.CC_STAT_AREA].tolist())

    count = len(region_colors)
    parent = list(range(count))
    edges = []
    for a, b in ((regions[:, :-1], regions[:, 1:]), (regions[:-1, :], regions[1:, :])):
        _check_cancel(cancelled)
        touching = (a != b) & (a != 0) & (b != 0)
        lo = np.minimum(a[touching], b[touching]).astype(np.uint64)
        hi = np.maximum(a[touching], b[touching]).astype(np.uint64)
        if lo.size:
            keys, contacts = np.unique(lo * np.uint64(count) + hi, return_counts=True)
            edges.extend((int(key // np.uint64(count)), int(key % np.uint64(count)), int(contact))
                         for key, contact in zip(keys, contacts))

    def root(value: int) -> int:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    # Prefer longer shared boundaries. The representative color is that of
    # the larger accumulated region; ties use component id for determinism.
    for index, (left, right, _) in enumerate(sorted(edges, key=lambda edge: (-edge[2], edge[0], edge[1]))):
        if index % 1024 == 0:
            _check_cancel(cancelled)
        left, right = root(left), root(right)
        if left == right:
            continue
        if float(distances[region_colors[left], region_colors[right]]) > threshold:
            continue
        if (region_sizes[left], -left) < (region_sizes[right], -right):
            left, right = right, left
        parent[right] = left
        region_sizes[left] += region_sizes[right]

    region_remap = np.zeros(count, dtype=np.uint8)
    for region in range(1, count):
        if region % 1024 == 0:
            _check_cancel(cancelled)
        region_remap[region] = region_colors[root(region)]
    labels[visible] = region_remap[regions[visible]]


def simplify_image(image: Image.Image, settings: CleanupSettings,
                   cancelled: Callable[[], bool] | None = None,
                   progress: Callable[[str], None] | None = None) -> CleanupResult:
    """Quantize without dithering, merge adjacent similar colors, then absorb tiny islands."""
    if not 2 <= settings.colors <= 256:
        raise ValueError("Color count must be between 2 and 256")
    if settings.target_width_mm <= 0 or settings.min_feature_mm < 0:
        raise ValueError("Print width must be positive and feature width non-negative")
    if settings.flat_min_area_px < 1 or not 0 <= settings.distinct_color_delta <= 100:
        raise ValueError("Invalid color protection settings")
    _check_cancel(cancelled)
    rgba = image.convert("RGBA")
    alpha = np.asarray(rgba.getchannel("A"), dtype=np.uint8)
    opaque = alpha > 0
    rgb_pixels = np.asarray(rgba.convert("RGB"), dtype=np.uint8).copy()
    width, height = image.size
    minimum_feature_px = max(1, math.ceil(width / settings.target_width_mm * settings.min_feature_mm))
    minimum_area = minimum_feature_px * minimum_feature_px
    if progress:
        progress("检测纯色区域")
    preserve_flat = settings.protect_distinct_colors if settings.preserve_flat_colors is None else settings.preserve_flat_colors
    flat_mask, flat_colors = (_flat_color_mask(rgb_pixels, opaque, max(minimum_area, settings.flat_min_area_px), cancelled)
                              if preserve_flat else (np.zeros(alpha.shape, dtype=bool), set()))
    _check_cancel(cancelled)
    if progress:
        progress("颜色量化")
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
    _check_cancel(cancelled)
    original_colors = len(used)
    palette = quantized.getpalette() or []
    labs = _lab_palette(palette, used) if used else {}
    distances = _lab_distances(labs)
    initial_labels = labels.copy()

    if progress:
        progress("合并相近色块")
    _merge_adjacent_regions(labels, opaque, labs, settings.color_delta, distances, cancelled)

    merged_islands = 0
    protected_islands = 0
    narrow_features = 0
    if progress:
        progress("检查并合并小色块")
    if minimum_area > 1:
        # Update labels after each merge so adjacent tiny components cannot
        # exchange colors simultaneously and remain fragmented.
        kernel = np.asarray([[0, 1, 0], [1, 1, 1], [0, 1, 0]], dtype=np.uint8)
        for color in sorted(int(v) for v in np.unique(labels[opaque])):
            _check_cancel(cancelled)
            mask = ((labels == color) & opaque).astype(np.uint8)
            count, components, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=4)
            for component_id in range(1, count):
                if component_id % 256 == 1:
                    _check_cancel(cancelled)
                area = int(stats[component_id, cv2.CC_STAT_AREA])
                x = int(stats[component_id, cv2.CC_STAT_LEFT])
                y = int(stats[component_id, cv2.CC_STAT_TOP])
                box_w = int(stats[component_id, cv2.CC_STAT_WIDTH])
                box_h = int(stats[component_id, cv2.CC_STAT_HEIGHT])
                x0, y0 = max(0, x - 1), max(0, y - 1)
                x1, y1 = min(width, x + box_w + 1), min(height, y + box_h + 1)
                component = components[y0:y1, x0:x1] == component_id
                if area >= minimum_area:
                    if min(box_w, box_h) < minimum_feature_px:
                        narrow_features += 1
                    else:
                        # Add background around image-edge components too;
                        # OpenCV otherwise has no outside pixels to measure.
                        padded = cv2.copyMakeBorder(component.astype(np.uint8), 1, 1, 1, 1,
                                                    cv2.BORDER_CONSTANT, value=0)
                        distance = cv2.distanceTransform(padded, cv2.DIST_L2, 3)
                        if float(distance.max()) * 2 < minimum_feature_px:
                            narrow_features += 1
                if area >= minimum_area:
                    continue
                local_opaque = opaque[y0:y1, x0:x1]
                boundary = (cv2.dilate(component.astype(np.uint8), kernel, iterations=1) > 0) & ~component & local_opaque
                contact = np.bincount(labels[y0:y1, x0:x1][boundary], minlength=256)
                contact[color] = 0
                candidates = [(int(n), int(contact[n])) for n in np.flatnonzero(contact)]
                if not candidates:
                    continue
                # A small but distinctly colored detail is often an eye, a
                # flower, or a printed feature; do not silently recolor it.
                if settings.protect_distinct_colors:
                    candidates = [pair for pair in candidates if distances[color, pair[0]] <= settings.distinct_color_delta]
                    if not candidates:
                        protected_islands += 1
                        continue
                chosen = min(
                    candidates,
                    key=lambda pair: (
                        float(distances[color, pair[0]]) / math.sqrt(pair[1]),
                        -pair[1],
                        pair[0],
                    ),
                )[0]
                labels[y0:y1, x0:x1][component] = chosen
                merged_islands += 1

    if progress:
        progress("生成处理结果")
    output_rgb = np.zeros((height, width, 3), dtype=np.uint8)
    for color in np.unique(labels[opaque]):
        _check_cancel(cancelled)
        color = int(color)
        output_rgb[(labels == color) & opaque] = palette[color * 3:color * 3 + 3]
    output_rgb[flat_mask] = rgb_pixels[flat_mask]
    if settings.smooth_edges:
        # Blend only the one-pixel boundary between different output colors.
        # Normalize by visibility so transparent pixels cannot introduce dark halos.
        edge = np.zeros((height, width), dtype=bool)
        horizontal = opaque[:, 1:] & opaque[:, :-1] & np.any(output_rgb[:, 1:] != output_rgb[:, :-1], axis=2)
        vertical = opaque[1:, :] & opaque[:-1, :] & np.any(output_rgb[1:, :] != output_rgb[:-1, :], axis=2)
        edge[:, 1:] |= horizontal
        edge[:, :-1] |= horizontal
        edge[1:, :] |= vertical
        edge[:-1, :] |= vertical
        smoothed = output_rgb.copy()
        # Process strips with a one-pixel halo to bound temporary memory.
        for top in range(0, height, 256):
            _check_cancel(cancelled)
            bottom = min(height, top + 256)
            start, end = max(0, top - 1), min(height, bottom + 1)
            visibility = opaque[start:end].astype(np.float32)
            weight = cv2.GaussianBlur(visibility, (3, 3), 0)
            blurred = cv2.GaussianBlur(output_rgb[start:end].astype(np.float32) * visibility[..., None], (3, 3), 0)
            smooth = np.rint(blurred / np.maximum(weight[..., None], 1e-6)).clip(0, 255).astype(np.uint8)
            selected = edge[top:bottom]
            smoothed[top:bottom][selected] = smooth[top - start:bottom - start][selected]
        output_rgb = smoothed
    output = Image.fromarray(np.dstack((output_rgb, alpha)), "RGBA")
    palette_rgb = np.asarray(palette, dtype=np.uint8).reshape(-1, 3)
    resulting_colors = flat_colors | {tuple(int(c) for c in palette_rgb[int(label)])
                                     for label in np.unique(labels[opaque & ~flat_mask])}
    quantized_changed = np.any(palette_rgb[initial_labels] != rgb_pixels, axis=2) & opaque & ~flat_mask
    merged_changed = (labels != initial_labels) & opaque & ~flat_mask
    original_rgb = rgb_pixels.astype(np.int16)
    changed = np.any(np.abs(output_rgb.astype(np.int16) - original_rgb) > 0, axis=2) & opaque
    changed_mask = Image.fromarray(changed.astype(np.uint8) * 255, "L")
    _check_cancel(cancelled)
    return CleanupResult(
        image=output,
        changed_mask=changed_mask,
        quantized_mask=Image.fromarray(quantized_changed.astype(np.uint8) * 255, "L"),
        merged_mask=Image.fromarray(merged_changed.astype(np.uint8) * 255, "L"),
        visible_pixels=int(np.count_nonzero(opaque)),
        quantized_pixels=int(np.count_nonzero(quantized_changed)),
        merged_pixels=int(np.count_nonzero(merged_changed)),
        original_colors=original_colors,
        resulting_colors=len(resulting_colors),
        merged_islands=merged_islands,
        protected_islands=protected_islands,
        preserved_flat_colors=len(flat_colors),
        minimum_feature_px=minimum_feature_px,
        narrow_features=narrow_features,
    )

