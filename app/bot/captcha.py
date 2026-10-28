"""Image CAPTCHA generation for join verification.

The code is derived from the verification token with an HMAC, so it survives
restarts without storing the plaintext answer in the database or in callback
data.  Only one of the inline keyboard options matches the image.
"""
from __future__ import annotations

import hashlib
import hmac
import random
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ..config import get_config

CHARSET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 5
OPTION_COUNT = 6

_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
)


def _digest(token: str, label: str) -> bytes:
    secret = (get_config().web.secret_key or "groupguard").encode("utf-8")
    message = f"{label}:{token}".encode()
    return hmac.new(secret, message, hashlib.sha256).digest()


def _code_from_bytes(data: bytes) -> str:
    return "".join(CHARSET[value % len(CHARSET)] for value in data[:CODE_LENGTH])


def captcha_code(token: str) -> str:
    """Return the correct code for a verification token."""
    return _code_from_bytes(_digest(token, "captcha-code"))


def captcha_options(token: str) -> list[str]:
    """Return the correct code plus deterministic distractors, shuffled."""
    correct = captcha_code(token)
    options = {correct}
    index = 0
    while len(options) < OPTION_COUNT:
        candidate = _code_from_bytes(_digest(token, f"captcha-option:{index}"))
        options.add(candidate)
        index += 1
    result = list(options)
    seed = int.from_bytes(_digest(token, "captcha-order")[:8], "big")
    random.Random(seed).shuffle(result)
    return result


def _load_font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    for raw in _FONT_CANDIDATES:
        path = Path(raw)
        if not path.exists():
            continue
        try:
            return ImageFont.truetype(str(path), size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def render_captcha(code: str) -> bytes:
    """Render a noisy PNG image containing *code* and return its bytes."""
    width, height = 380, 150
    rng = random.Random()
    background = (rng.randint(238, 255), rng.randint(238, 255), rng.randint(238, 255))
    image = Image.new("RGB", (width, height), background)
    draw = ImageDraw.Draw(image)

    # Background interference.
    for _ in range(12):
        color = (rng.randint(150, 215), rng.randint(150, 215), rng.randint(150, 215))
        draw.line(
            [
                (rng.randint(0, width), rng.randint(0, height)),
                (rng.randint(0, width), rng.randint(0, height)),
            ],
            fill=color,
            width=rng.randint(1, 2),
        )
    for _ in range(260):
        color = (rng.randint(140, 220), rng.randint(140, 220), rng.randint(140, 220))
        x, y = rng.randint(0, width - 1), rng.randint(0, height - 1)
        draw.point((x, y), fill=color)

    step = (width - 40) // max(1, len(code))
    font_size = max(36, min(74, int(step * 1.05)))
    font = _load_font(font_size)

    for index, char in enumerate(code):
        color = (rng.randint(0, 90), rng.randint(0, 90), rng.randint(0, 90))
        bbox = draw.textbbox((0, 0), char, font=font)
        char_width = bbox[2] - bbox[0]
        char_height = bbox[3] - bbox[1]
        char_image = Image.new(
            "RGBA", (char_width + 54, char_height + 54), (0, 0, 0, 0)
        )
        char_draw = ImageDraw.Draw(char_image)
        char_draw.text(
            (27 - bbox[0], 27 - bbox[1]),
            char,
            font=font,
            fill=(*color, 255),
        )
        char_image = char_image.rotate(
            rng.uniform(-26, 26), resample=Image.BICUBIC, expand=1
        )
        x = 18 + index * step + rng.randint(-4, 4)
        y = (height - char_image.height) // 2 + rng.randint(-9, 9)
        image.paste(char_image, (x, y), char_image)

    # Foreground interference, drawn after the text.
    for _ in range(7):
        color = (rng.randint(60, 150), rng.randint(60, 150), rng.randint(60, 150))
        draw.line(
            [
                (rng.randint(0, width), rng.randint(0, height)),
                (rng.randint(0, width), rng.randint(0, height)),
            ],
            fill=color,
            width=rng.randint(1, 2),
        )
    for _ in range(160):
        color = (rng.randint(100, 190), rng.randint(100, 190), rng.randint(100, 190))
        x, y = rng.randint(0, width - 1), rng.randint(0, height - 1)
        draw.ellipse((x, y, x + 1, y + 1), fill=color)

    draw.rectangle((1, 1, width - 2, height - 2), outline=(160, 160, 160), width=2)
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()
