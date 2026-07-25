"""Generate assets/icon.ico — two overlapping file shapes with a diff mark.

Run once (requires Pillow):  python scripts/make_icon.py
The generated icon is committed so normal builds don't need Pillow.
"""

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "assets" / "icon.ico"

BACK = (30, 41, 59, 255)       # slate
FILE_1 = (96, 165, 250, 255)   # blue
FILE_2 = (52, 211, 153, 255)   # green
MARK = (250, 204, 21, 255)     # yellow


def draw_file(draw, x, y, w, h, color):
    fold = w // 3
    draw.polygon(
        [
            (x, y),
            (x + w - fold, y),
            (x + w, y + fold),
            (x + w, y + h),
            (x, y + h),
        ],
        fill=color,
    )
    draw.polygon(
        [(x + w - fold, y), (x + w - fold, y + fold), (x + w, y + fold)],
        fill=tuple(min(255, c + 60) for c in color[:3]) + (255,),
    )


def make(size):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    r = size // 8
    draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=r, fill=BACK)
    u = size / 16
    draw_file(draw, int(2.5 * u), int(2.5 * u), int(7 * u), int(9 * u), FILE_1)
    draw_file(draw, int(6.5 * u), int(4.5 * u), int(7 * u), int(9 * u), FILE_2)
    cx, cy, cr = int(11.5 * u), int(11.5 * u), int(2.6 * u)
    draw.ellipse([cx - cr, cy - cr, cx + cr, cy + cr], fill=MARK)
    bar = max(1, int(0.6 * u))
    draw.rectangle([cx - cr + bar * 2, cy - bar, cx + cr - bar * 2, cy + bar], fill=BACK)
    return img


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    sizes = [16, 24, 32, 48, 64, 128, 256]
    images = [make(s) for s in sizes]
    images[-1].save(OUT, format="ICO", sizes=[(s, s) for s in sizes])
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
