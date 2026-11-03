import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageCms

from lumina_pendant_prep.image_io import open_rgba
from lumina_pendant_prep.project import load_project, save_project


class ImageAndProjectTests(unittest.TestCase):
    def test_icc_palette_png_preserves_transparency(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "palette.png"
            image = Image.new("P", (2, 1))
            image.putpalette([255, 0, 0, 0, 0, 255] + [0] * 762)
            image.putdata([0, 1])
            profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
            image.save(path, transparency=0, icc_profile=profile)
            loaded = open_rgba(path)
            self.assertEqual(loaded.getpixel((0, 0))[3], 0)
            self.assertEqual(loaded.getpixel((1, 0))[3], 255)

    def test_png_load_and_exif_orientation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rotated.png"
            image = Image.new("RGB", (12, 20), "red")
            exif = Image.Exif()
            exif[274] = 6
            image.save(path, exif=exif)
            loaded = open_rgba(path)
            self.assertEqual(loaded.mode, "RGBA")
            self.assertEqual(loaded.size, (20, 12))

    def test_common_formats_and_heic_decode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sample = Image.new("RGB", (16, 12), (125, 80, 30))
            for suffix, format_name in (("jpg", "JPEG"), ("png", "PNG"), ("webp", "WEBP"),
                                        ("bmp", "BMP"), ("tif", "TIFF")):
                path = root / f"sample.{suffix}"
                sample.save(path, format=format_name)
                self.assertEqual(open_rgba(path).size, sample.size, suffix)

            heic_path = root / "sample.heic"
            sample.save(heic_path, format="HEIF", quality=80)
            self.assertEqual(open_rgba(heic_path).size, sample.size)

    def test_project_round_trip(self):
        state = {
            "source_path": "photo.heic", "crop": [0.1, 0.2, 0.9, 0.95],
            "hole": [0.5, 0.3], "shape": "圆形", "polygon": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pendant.lpproj"
            save_project(path, state)
            self.assertEqual(load_project(path)["crop"], state["crop"])
            self.assertEqual(load_project(path)["shape"], state["shape"])


if __name__ == "__main__":
    unittest.main()
