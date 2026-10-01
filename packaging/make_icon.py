"""assets/icon.png -> assets/LAFhvst.ico (다중 해상도) 생성.

사용:
    python packaging/make_icon.py
"""

import os

from PIL import Image

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BASE_DIR, "assets", "icon.png")
DST = os.path.join(BASE_DIR, "assets", "LAFhvst.ico")
SIZES = [16, 24, 32, 48, 64, 128, 256]


def main() -> None:
    image = Image.open(SRC).convert("RGBA")
    image.save(DST, format="ICO", sizes=[(size, size) for size in SIZES])
    print("wrote", DST)


if __name__ == "__main__":
    main()
