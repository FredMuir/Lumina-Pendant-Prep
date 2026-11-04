"""Luminance-based height maps aligned to the rendered color image."""
from PIL import Image, ImageChops, ImageOps


def make_heightmap(image: Image.Image, dark_high: bool = False, strength: float = 1.0) -> Image.Image:
    """Return an 8-bit Lumina height map; transparent areas become black.

    Alpha fades partially transparent edge pixels toward zero. This is a
    luminance mapping, not a reconstruction of the scene's physical depth.
    """
    rgba = image.convert("RGBA")
    luminance = rgba.convert("L")
    if dark_high:
        luminance = ImageOps.invert(luminance)
    if not 0 <= strength <= 2:
        raise ValueError("Heightmap strength must be between 0 and 2")
    if strength != 1:
        luminance = luminance.point(lambda value: min(255, round(value * strength)))
    result = ImageChops.multiply(luminance, rgba.getchannel("A"))
    # Pillow operations may copy an RGB ICC profile into the grayscale image;
    # do not label single-channel height data with a color-image profile.
    result.info.clear()
    result.info["dpi"] = image.info.get("dpi", (300, 300))
    return result
