"""Render SVGs with QuickLook on black and white, and compute transparency."""
import subprocess
import sys
from pathlib import Path

from PIL import Image


def render(svg: Path) -> Image.Image:
    text = svg.read_text()
    out = {}
    for bg in ("black", "white"):
        colored = svg.with_name(f"{svg.stem}_{bg}.svg")
        fill = "#000000" if bg == "black" else "#FFFFFF"
        head, _, rest = text.partition(">")
        colored.write_text(f'{head}><rect width="512" height="512" fill="{fill}"/>{rest}')
        subprocess.run(
            ["qlmanage", "-t", "-s", "1024", "-o", str(svg.parent), str(colored)],
            capture_output=True, check=True,
        )
        out[bg] = Image.open(f"{colored}.png").convert("RGB")
    black, white = out["black"], out["white"]
    image = Image.new("RGBA", black.size)
    bp, wp, op = black.load(), white.load(), image.load()
    for y in range(black.height):
        for x in range(black.width):
            b, w = bp[x, y], wp[x, y]
            alpha = max(0, min(255, 255 - max(w[i] - b[i] for i in range(3))))
            op[x, y] = (0, 0, 0, 0) if alpha == 0 else tuple(
                min(255, round(b[i] * 255 / alpha)) for i in range(3)
            ) + (alpha,)
    return image


if __name__ == "__main__":
    for path in sys.argv[1:]:
        svg = Path(path)
        image = render(svg)
        image.resize((512, 512), Image.LANCZOS).save(svg.with_suffix(".512.png"))
        image.resize((256, 256), Image.LANCZOS).save(svg.with_suffix(".256.png"))
