"""Draws Demo's fictional logo, mark and favicon (brown + beige) so the demo carries no real branding."""
import sys
# Usage: python tools/make_logo.py <path to a Fraunces .ttf/.woff> assets
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

FONT = sys.argv[1]
OUT = Path(sys.argv[2])
BROWN, DEEP, BEIGE, CARAMEL = (122, 74, 38), (94, 56, 25), (247, 240, 230), (169, 113, 63)


def mark(size: int) -> Image.Image:
    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.22), fill=BROWN)
    # an orbit behind the letter: a quiet nod to quantum without any product detail
    d.ellipse([s * 0.12, s * 0.36, s * 0.88, s * 0.64], outline=(226, 176, 126), width=max(2, s // 48))
    font = ImageFont.truetype(FONT, int(s * 0.62))
    box = d.textbbox((0, 0), "D", font=font)
    w, h = box[2] - box[0], box[3] - box[1]
    d.text(((s - w) / 2 - box[0], (s - h) / 2 - box[1] - s * 0.01), "D", font=font, fill=BEIGE)
    r = s * 0.045
    cx, cy = s * 0.84, s * 0.44
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(226, 176, 126))
    return img.resize((size, size), Image.LANCZOS)


def logo() -> Image.Image:
    """The mark plus the word 'Demo' — the company's whole name."""
    W, H = 760 * 2, 260 * 2
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    m = mark(H - 80)
    img.alpha_composite(m, (20, 40))
    d = ImageDraw.Draw(img)
    big = ImageFont.truetype(FONT, int(H * 0.56))
    box = d.textbbox((0, 0), "Demo", font=big)
    x = H + 30
    y = (H - (box[3] - box[1])) / 2 - box[1]
    d.text((x, y), "Demo", font=big, fill=CARAMEL)
    right = int(x + d.textlength("Demo", font=big)) + 24
    return img.crop((0, 0, right, H)).resize((right // 2, H // 2), Image.LANCZOS)


OUT.mkdir(parents=True, exist_ok=True)
logo().save(OUT / "demo_logo.png")
mark(256).save(OUT / "demo_mark.png")
mark(64).save(OUT / "favicon.png")
print("written to", OUT)
