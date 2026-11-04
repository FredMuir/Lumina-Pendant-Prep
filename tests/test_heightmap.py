import unittest
from pathlib import Path

from PIL import Image

from lumina_pendant_prep.heightmap import make_heightmap
from lumina_pendant_prep.render import PendantSettings, make_pendant


class HeightmapTests(unittest.TestCase):
    def test_direction_alpha_and_partial_edge(self):
        image = Image.new("RGBA", (4, 1))
        image.putdata([(0, 0, 0, 255), (255, 255, 255, 255),
                       (255, 255, 255, 0), (255, 255, 255, 128)])
        light = make_heightmap(image)
        dark = make_heightmap(image, dark_high=True)
        self.assertEqual(light.mode, "L")
        self.assertEqual(list(light.tobytes()), [0, 255, 0, 128])
        self.assertEqual(list(dark.tobytes()), [255, 0, 0, 0])

    def test_real_photo_geometry_and_hole_align(self):
        with Image.open(Path(__file__).parent / "fixtures" / "cat-photo.jpg") as photo:
            source = photo.convert("RGBA")
            source.thumbnail((320, 320))
        color = make_pendant(source, PendantSettings(crop=(0, 0, 1, 1),
                             shape="Rounded rectangle", hole_enabled=True))
        height = make_heightmap(color, dark_high=True)
        self.assertEqual(height.size, color.size)
        self.assertEqual(height.getpixel((0, 0)), 0)
        x, y = round(color.width * .5), round(color.height * .12)
        self.assertEqual(color.getpixel((x, y))[3], 0)
        self.assertEqual(height.getpixel((x, y)), 0)


if __name__ == "__main__":
    unittest.main()
