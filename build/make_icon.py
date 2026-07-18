"""Generate a simple app icon: dark gradient circle with a bold 'rc' monogram.
Produces build/icon.png (512x512) and build/icon.ico (multi-resolution).
"""
from PIL import Image, ImageDraw, ImageFont
from pathlib import Path

HERE = Path(__file__).parent
SIZE = 512

# Colours — matches the UI accent palette
BG_TOP = (30, 38, 58)          # deep navy
BG_BOT = (58, 76, 130)         # indigo
FG = (230, 233, 239)           # near-white
ACCENT = (122, 162, 247)       # accent blue


def radial_gradient(size, inner, outer):
    """Simple radial gradient from inner (centre) to outer (edge)."""
    img = Image.new("RGBA", (size, size), outer + (255,))
    draw = ImageDraw.Draw(img)
    cx = cy = size / 2
    max_r = size / 2
    # Draw from outer to inner in concentric circles
    steps = 80
    for i in range(steps, 0, -1):
        r = (i / steps) * max_r
        t = 1 - (i / steps)  # 0 at edge, 1 at centre
        rc = int(outer[0] + (inner[0] - outer[0]) * t)
        gc = int(outer[1] + (inner[1] - outer[1]) * t)
        bc = int(outer[2] + (inner[2] - outer[2]) * t)
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(rc, gc, bc, 255))
    return img


def load_font(size):
    # Try a few common fonts; fall back to default
    candidates = [
        "C:/Windows/Fonts/segoeuib.ttf",
        "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def make_icon():
    # Background: circular dark gradient, transparent outside the circle
    canvas = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))

    # Draw gradient square, then mask to a circle
    grad = radial_gradient(SIZE, BG_BOT, BG_TOP)
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, SIZE, SIZE), fill=255)
    canvas.paste(grad, (0, 0), mask)

    # Subtle inner highlight ring
    ring = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    ImageDraw.Draw(ring).ellipse(
        (SIZE * 0.06, SIZE * 0.06, SIZE * 0.94, SIZE * 0.94),
        outline=(255, 255, 255, 28),
        width=3,
    )
    canvas.alpha_composite(ring)

    # Monogram
    text = "rc"
    font = load_font(int(SIZE * 0.52))
    draw = ImageDraw.Draw(canvas)
    # Centre the text
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    x = (SIZE - tw) / 2 - bbox[0]
    y = (SIZE - th) / 2 - bbox[1] - SIZE * 0.02  # nudge up slightly
    # Soft shadow
    shadow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).text((x + 4, y + 6), text, font=font, fill=(0, 0, 0, 120))
    canvas.alpha_composite(shadow)
    # Main text
    draw.text((x, y), text, font=font, fill=FG + (255,))
    # Accent underline / dot — single dot under the 'c' for a subtle visual anchor
    dot_r = SIZE * 0.03
    dx = SIZE / 2 + SIZE * 0.21
    dy = y + th + SIZE * 0.02
    draw.ellipse((dx - dot_r, dy - dot_r, dx + dot_r, dy + dot_r), fill=ACCENT + (255,))

    out_png = HERE / "icon.png"
    canvas.save(out_png, "PNG")
    print("wrote", out_png)

    # Multi-resolution .ico
    out_ico = HERE / "icon.ico"
    sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]
    canvas.save(out_ico, format="ICO", sizes=sizes)
    print("wrote", out_ico)


if __name__ == "__main__":
    make_icon()
