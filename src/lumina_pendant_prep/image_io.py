from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageCms, ImageOps

try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
except ImportError:
    pass


SUPPORTED_SUFFIXES = {
    ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff",
    ".heic", ".heif", ".avif",
}


def open_rgba(path: str | Path) -> Image.Image:
    """Decode a still image, apply its EXIF orientation and normalize to sRGB RGBA."""
    path = Path(path)
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise ValueError(f"Unsupported image extension: {path.suffix or '(none)'}")
    with Image.open(path) as opened:
        opened.seek(0)  # Multi-image HEIF/TIFF inputs use their primary/first frame.
        image = ImageOps.exif_transpose(opened)
        icc = image.info.get("icc_profile")
        if icc:
            try:
                source_profile = ImageCms.ImageCmsProfile(bytes(icc))
                target_profile = ImageCms.createProfile("sRGB")
                alpha = image.getchannel("A") if "A" in image.getbands() else None
                base = image.convert("RGB") if alpha is not None or image.mode == "P" else image
                rgb = ImageCms.profileToProfile(
                    base, source_profile, target_profile, outputMode="RGB"
                )
                image = rgb.convert("RGBA")
                if alpha is not None:
                    image.putalpha(alpha)
            except (OSError, ValueError):
                image = image.convert("RGBA")
        else:
            image = image.convert("RGBA")
        return image.copy()

