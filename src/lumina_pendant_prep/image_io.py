from __future__ import annotations

from pathlib import Path
from io import BytesIO

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

SRGB_PROFILE = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def open_rgba(path: str | Path) -> Image.Image:
    """Decode a still image, apply its EXIF orientation and normalize to sRGB RGBA."""
    path = Path(path)
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise ValueError(f"Unsupported image extension: {path.suffix or '(none)'}")
    with Image.open(path) as opened:
        opened.seek(0)  # Multi-image HEIF/TIFF inputs use their primary/first frame.
        image = ImageOps.exif_transpose(opened)
        icc = image.info.get("icc_profile")
        color_status = "assumed_srgb"
        if icc:
            try:
                source_profile = ImageCms.ImageCmsProfile(BytesIO(icc))
                target_profile = ImageCms.createProfile("sRGB")
                has_alpha = "A" in image.getbands() or "transparency" in image.info
                alpha = image.convert("RGBA").getchannel("A") if has_alpha else None
                base = image.convert("RGB") if alpha is not None or image.mode == "P" else image
                rgb = ImageCms.profileToProfile(
                    base, source_profile, target_profile, outputMode="RGB"
                )
                image = rgb.convert("RGBA")
                if alpha is not None:
                    image.putalpha(alpha)
                color_status = "converted_srgb"
            except (OSError, ValueError, ImageCms.PyCMSError):
                image = image.convert("RGBA")
                color_status = "invalid_icc"
        else:
            image = image.convert("RGBA")
        # Never carry an invalid or source-space profile into sRGB output.
        image.info["icc_profile"] = SRGB_PROFILE
        image.info["lpp_color_status"] = color_status
        return image.copy()

