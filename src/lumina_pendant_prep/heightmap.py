"""Luminance-based height maps aligned to the rendered color image."""
from PIL import Image, ImageChops, ImageOps


def make_heightmap(image: Image.Image, dark_high: bool = False) -> Image.Image:
    """Return an 8-bit Lumina height map; transparent areas become black.

    Alpha fades partially transparent edge pixels toward zero. This is a
    luminance mapping, not a reconstruction of the scene's physical depth.
    """
    rgba = image.convert("RGBA")
    luminance = rgba.convert("L")
    if dark_high:
        luminance = ImageOps.invert(luminance)
    result = ImageChops.multiply(luminance, rgba.getchannel("A"))
    # Pillow operations may copy an RGB ICC profile into the grayscale image;
    # do not label single-channel height data with a color-image profile.
    result.info.clear()
    result.info["dpi"] = image.info.get("dpi", (300, 300))
    return result
