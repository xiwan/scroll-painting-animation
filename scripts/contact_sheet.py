"""精灵对照表：把一批透明 PNG 按网格排在纸色底上、标名字，用来一眼检查风格是否统一、朝向（门脸朝左下还是右下）、
有没有抠图残渣、有没有混进文字。长边控制在 1900 以内，数量多时自动分页。

用法:
  python contact_sheet.py "assets/cut/bld_*.png" --cols 8 --out /tmp/sheet.jpg
  python contact_sheet.py a.png b.png c.png --cols 3 --tile 380x300 --bg 236,226,204
"""
import argparse
import glob
import os

from PIL import Image, ImageDraw


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+", help="文件或通配符")
    ap.add_argument("--cols", type=int, default=6)
    ap.add_argument("--tile", default="300x260")
    ap.add_argument("--bg", default="236,226,204")
    ap.add_argument("--max", type=int, default=1900)
    ap.add_argument("--out", default="/tmp/contact_sheet.jpg")
    a = ap.parse_args()
    files = []
    for f in a.files:
        files += sorted(glob.glob(f)) if any(c in f for c in "*?[") else [f]
    if not files:
        raise SystemExit("没有文件")
    tw, th = (int(v) for v in a.tile.split("x"))
    cols = a.cols
    tw = min(tw, a.max // cols)
    rows_per = max(1, a.max // th)
    bg = tuple(int(v) for v in a.bg.split(","))
    pages = [files[i:i + cols * rows_per] for i in range(0, len(files), cols * rows_per)]
    stem, ext = os.path.splitext(a.out)
    for pi, page in enumerate(pages):
        rows = (len(page) + cols - 1) // cols
        o = Image.new("RGB", (tw * cols, th * rows), bg)
        d = ImageDraw.Draw(o)
        for i, f in enumerate(page):
            im = Image.open(f).convert("RGBA")
            im.thumbnail((tw - 10, th - 24))
            x, y = (i % cols) * tw, (i // cols) * th
            o.paste(im, (x + (tw - im.width) // 2, y + 20), im)
            d.text((x + 4, y + 3), f"{i} {os.path.splitext(os.path.basename(f))[0]}", fill=(0, 0, 0))
        p = a.out if len(pages) == 1 else f"{stem}_{pi}{ext}"
        o.save(p, quality=88)
        print(p, o.size, len(page), "张")


if __name__ == "__main__":
    main()
