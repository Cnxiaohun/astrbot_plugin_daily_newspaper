"""Manual transport conversion preserves source and never changes dimensions."""
import tempfile
import unittest
from pathlib import Path
from PIL import Image

from core.delivery import jpeg_delivery_copy


class JpegDeliveryTests(unittest.TestCase):
    def test_keeps_original_dimensions_and_bytes(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "image.png"
            Image.new("RGBA", (1024, 1536), (255, 255, 255, 0)).save(source)
            before = source.read_bytes()
            output = jpeg_delivery_copy(source)
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(output.name, "delivery-jpeg.jpg")
            with Image.open(output) as image:
                self.assertEqual(image.format, "JPEG")
                self.assertEqual(image.size, (1024, 1536))
                self.assertEqual(image.mode, "RGB")
                self.assertEqual(image.getpixel((0, 0)), (255, 255, 255))

    def test_invalid_input_never_replaces_source(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "image.png"
            source.write_bytes(b"not an image")
            with self.assertRaises(Exception):
                jpeg_delivery_copy(source)
            self.assertEqual(source.read_bytes(), b"not an image")
            self.assertFalse((Path(root) / "delivery-jpeg.jpg").exists())
