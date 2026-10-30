"""Shrinks an app icon to what craft-app.json accepts: a PNG of at most 512 px a side and 256 KB.

For maintainers preparing a bridge (bridge.py itself needs no third-party packages). Needs Pillow:

    python -m pip install pillow
    python tools/icon.py upstream-icon.png out.png [--size 256]

The result can be committed to the bridge repo and pointed at with
`[icon] url = "https://raw.githubusercontent.com/<bridge repo>/main/out.png"`.
"""

import argparse
import io
import sys

from PIL import Image

MAX_BYTES = 256 * 1024
MAX_EDGE = 512


def shrink(src: str, size: int) -> bytes:
    im = Image.open(src)
    im.load()
    im = im.convert("RGBA")
    edge = min(size, MAX_EDGE)
    while True:
        copy = im.copy()
        copy.thumbnail((edge, edge), Image.LANCZOS)
        out = io.BytesIO()
        copy.save(out, "PNG", optimize=True)
        data = out.getvalue()
        if len(data) <= MAX_BYTES or edge <= 32:
            return data
        edge = int(edge * 0.8)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--size", type=int, default=256, help="largest edge in pixels (at most 512)")
    a = ap.parse_args()
    data = shrink(a.src, a.size)
    if len(data) > MAX_BYTES:
        print(f"couldn't get {a.src} under {MAX_BYTES} bytes", file=sys.stderr)
        return 1
    with open(a.dst, "wb") as f:
        f.write(data)
    w, h = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    print(f"{a.dst}: {w}x{h}, {len(data)} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
