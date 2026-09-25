import unittest

from PIL import Image

from companion.calibration import find_strip
from companion.protocol import encode, render
from companion.visual import encode_control


class CalibrationTests(unittest.TestCase):
    def fixture(self, frame, scale):
        width, height = round(512 * scale), round(16 * scale)
        strip = render(frame).resize((width, height), Image.Resampling.NEAREST)
        canvas = Image.new('RGB', (1600, 200), '#18222d')
        left, top = round(8 * scale), round(8 * scale)
        canvas.paste(strip, (left, top))
        return canvas, (left, top, width, height)

    def test_finds_scaled_prompt_strip(self):
        image, expected = self.fixture(encode('calibrate')[0], 1.875)
        self.assertEqual(find_strip(image), expected)

    def test_finds_scaled_return_control(self):
        image, expected = self.fixture(encode_control(kind='slot'), 1.5)
        self.assertEqual(find_strip(image), expected)


if __name__ == '__main__':
    unittest.main()
