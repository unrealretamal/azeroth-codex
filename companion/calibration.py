"""Find the top-left optical strip across common UI/display scale factors."""
from .protocol import decode_image

BASE_LEFT = 8
BASE_TOP = 8
BASE_WIDTH = 512
BASE_HEIGHT = 16


def find_strip(image, scales=None, slack=6):
    """Return ``(left, top, width, height)`` for a checksum-valid strip.

    The addon anchors its strip at 8 UI units.  Desktop composition can scale it
    by fractional factors (for example 1.875 on a 2560x1440 display), so only
    candidates that decode a complete checked frame are accepted.
    """
    if scales is None:
        scales = tuple(step / 8 for step in range(4, 25))
    for scale in scales:
        width = round(BASE_WIDTH * scale)
        height = round(BASE_HEIGHT * scale)
        expected_left = round(BASE_LEFT * scale)
        expected_top = round(BASE_TOP * scale)
        origins = [(left, top)
                   for top in range(max(0, expected_top - slack), expected_top + slack + 1)
                   for left in range(max(0, expected_left - slack), expected_left + slack + 1)]
        origins.sort(key=lambda point: (abs(point[0] - expected_left) + abs(point[1] - expected_top), point))
        for left, top in origins:
                if left + width > image.width or top + height > image.height:
                    continue
                try:
                    decode_image(image.crop((left, top, left + width, top + height)))
                except (ValueError, UnicodeError):
                    continue
                return left, top, width, height
    raise ValueError('No valid strip found. Keep WoW visible and unobscured, then retry.')
