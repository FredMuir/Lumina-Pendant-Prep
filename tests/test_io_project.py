import tempfile
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageCms

from lumina_pendant_prep.image_io import open_rgba, SRGB_PROFILE
from lumina_pendant_prep.project import load_project, save_project, source_path_for_project, resolve_project_source


class ImageAndProjectTests(unittest.TestCase):
    def test_multiframe_photo_heif_selects_primary_and_tiff_first_page(self):
        fixtures = Path(__file__).parent / "fixtures"
        with Image.open(fixtures / "cat-photo.jpg") as cat, Image.open(fixtures / "flower-photo.jpg") as flower:
            images = [cat.convert("RGB").resize((160, 120)), flower.convert("RGB").resize((160, 120))]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            heif = root / "photos.heic"
            images[0].save(heif, format="HEIF", save_all=True, append_images=[images[1]], primary_index=1)
            with Image.open(heif) as raw:
                self.assertEqual(raw.n_frames, 2)
                self.assertEqual(raw.tell(), 1)
                expected = raw.convert("RGBA")
            actual = open_rgba(heif)
            self.assertEqual(actual.info["lpp_frame_count"], 2)
            self.assertEqual(actual.info["lpp_frame_index"], 1)
            self.assertTrue(actual.info["lpp_frame_is_primary"])
            self.assertEqual(actual.tobytes(), expected.tobytes())
            tiff = root / "photos.tiff"
            images[0].save(tiff, format="TIFF", save_all=True, append_images=[images[1]])
            actual = open_rgba(tiff)
            self.assertEqual(actual.info["lpp_frame_count"], 2)
            self.assertEqual(actual.info["lpp_frame_index"], 0)
            self.assertFalse(actual.info["lpp_frame_is_primary"])
            self.assertEqual(actual.tobytes(), images[0].convert("RGBA").tobytes())

    def test_real_photo_format_derivatives_decode_with_consistent_content(self):
        fixture = Path(__file__).parent / "fixtures" / "cat-photo.jpg"
        with Image.open(fixture) as original:
            photo = original.convert("RGB").resize((320, 180), Image.Resampling.LANCZOS)
        baseline = np.asarray(photo, dtype=np.int16)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            formats = (("jpg", "JPEG"), ("png", "PNG"), ("webp", "WEBP"),
                       ("bmp", "BMP"), ("tif", "TIFF"), ("heic", "HEIF"),
                       ("heif", "HEIF"), ("avif", "AVIF"))
            for suffix, format_name in formats:
                with self.subTest(suffix=suffix):
                    path = root / f"photo.{suffix}"
                    photo.save(path, format=format_name)
                    decoded = open_rgba(path)
                    self.assertEqual(decoded.size, photo.size)
                    self.assertEqual(decoded.mode, "RGBA")
                    self.assertEqual(decoded.getchannel("A").getextrema(), (255, 255))
                    difference = np.abs(np.asarray(decoded.convert("RGB"), dtype=np.int16) - baseline)
                    self.assertLess(float(difference.mean()), 15.0)

    def test_real_photo_exif_orientation_and_transparent_png(self):
        fixture = Path(__file__).parent / "fixtures" / "cat-photo.jpg"
        with Image.open(fixture) as original:
            photo = original.convert("RGB").resize((320, 180), Image.Resampling.LANCZOS)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            oriented = root / "rotated.jpg"
            exif = Image.Exif()
            exif[274] = 6
            photo.save(oriented, exif=exif, quality=95)
            result = open_rgba(oriented)
            self.assertEqual(result.size, (180, 320))
            # Compare with the actual decoded JPEG rotated clockwise, avoiding
            # assumptions about lossy JPEG compression.
            with Image.open(oriented) as raw:
                expected = raw.convert("RGBA").transpose(Image.Transpose.ROTATE_270)
            self.assertEqual(result.tobytes(), expected.tobytes())
            rgba = photo.convert("RGBA")
            rgba.putalpha(Image.new("L", photo.size, 180))
            rgba.putpixel((5, 5), (*rgba.getpixel((5, 5))[:3], 0))
            transparent = root / "transparent.png"
            profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
            rgba.save(transparent, icc_profile=profile)
            loaded = open_rgba(transparent)
            self.assertEqual(loaded.getchannel("A").tobytes(), rgba.getchannel("A").tobytes())
            self.assertEqual(loaded.info["lpp_color_status"], "converted_srgb")

    def test_real_photo_invalid_icc_reports_fallback(self):
        fixture = Path(__file__).parent / "fixtures" / "cat-photo.jpg"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid-profile.png"
            with Image.open(fixture) as photo:
                photo.resize((320, 180), Image.Resampling.LANCZOS).save(path, icc_profile=b"invalid profile")
            loaded = open_rgba(path)
            self.assertEqual(loaded.info["lpp_color_status"], "invalid_icc")
            self.assertEqual(loaded.info["icc_profile"], SRGB_PROFILE)

    def test_project_and_photo_can_move_together(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "original"
            original.mkdir()
            photo = original / "photo.png"
            Image.new("RGB", (4, 4), "red").save(photo)
            project = original / "pendant.lpproj"
            save_project(project, {"source_path": source_path_for_project(project, photo)})
            moved = root / "moved"
            shutil.move(str(original), str(moved))
            moved_project = moved / project.name
            source = resolve_project_source(moved_project, load_project(moved_project)["source_path"])
            self.assertTrue(Path(source).samefile(moved / photo.name))
            self.assertEqual(open_rgba(source).size, (4, 4))

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

    def test_rejects_invalid_project_geometry_before_opening_image(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.lpproj"
            save_project(path, {"source_path": "photo.png", "crop": [0, 0, 0, 1]})
            with self.assertRaisesRegex(ValueError, "crop"):
                load_project(path)
            save_project(path, {"source_path": "photo.png", "hole_compensation_mm": -0.3})
            with self.assertRaisesRegex(ValueError, "hole_compensation_mm"):
                load_project(path)

    def test_failed_project_replace_preserves_previous_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "pendant.lpproj"
            save_project(path, {"source_path": "original.png"})
            with patch("lumina_pendant_prep.project.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaises(OSError):
                    save_project(path, {"source_path": "new.png"})
            self.assertEqual(load_project(path)["source_path"], "original.png")
            self.assertEqual(list(root.iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
