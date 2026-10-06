"""把生成的俯视地面/墙面图做成无缝贴图（本机处理，不要用扩散模型去"修接缝"——会留下十字形的缝）。

模式：
  bond   错缝石板/砖墙：按横缝切成一行行，每行在两端各找一条竖缝截断，再统一拉到同一宽度 → 每行左右首尾是缝，
         上下边也是缝；每行再各自随机平移，贴图边上不会竖着对齐一道缝
  grid   方砖网格：横竖都在砖缝上裁 → 网格线连续
  soft   土面/草地这类只有低频色块的：平移半张后与原图用平滑窗口交叉淡化
  wrapx  只沿横向首尾相接（路面条带：纵向是路的横断面，不能平铺）

用法:
  python tex_tile.py in.png out.png --mode bond [--crop 0,.24,1,1] [--rows 12]
  python tex_tile.py in.png out.png --mode soft --check check.jpg      # 另出一张 2×2 平铺图，肉眼查接缝
"""
import argparse

import numpy as np
from PIL import Image, ImageFilter


def lum(a):
    return a[..., :3].astype(np.float32) @ np.array([.3, .59, .11], np.float32)


def smooth(p, k):
    k = max(1, int(k))
    return np.convolve(np.pad(p, k, mode="edge"), np.ones(2 * k + 1) / (2 * k + 1), "same")[k:-k]


def period(p, lo, hi):
    p = p - p.mean()
    ac = np.correlate(p, p, "full")[len(p) - 1:]
    hi = min(hi, len(ac) - 1)
    return lo + int(np.argmax(ac[lo:hi]))


def joints(p, per):
    """暗线（缝）位置：在每个周期窗口里找最小值。"""
    p = smooth(p, per * .06)
    out, x = [], int(np.argmin(p[:per]))
    while x < len(p):
        out.append(x)
        a, b = x + int(per * .6), x + int(per * 1.4)
        if a >= len(p):
            break
        x = a + int(np.argmin(p[a:min(b, len(p))]))
    return out


def running_bond(src, n_rows=None):
    a = np.asarray(src, np.float32)
    L = lum(a)
    ry = period(L.mean(1), 12, 200)
    ys = joints(L.mean(1), ry)
    rows = []
    for y0, y1 in zip(ys[:-1], ys[1:]):
        if y1 - y0 < ry * .6:
            continue
        strip = a[y0:y1]
        q = lum(strip[int((y1 - y0) * .25):int((y1 - y0) * .75)]).mean(0)
        xs = joints(q, period(q, 20, 400))
        if len(xs) >= 3:
            rows.append(strip[:, xs[0]:xs[-1]])
    if not rows:
        raise SystemExit("没找到规整的砖行：换 soft 模式，或先裁掉杂乱区域（--crop）")
    wm = np.median([r.shape[1] for r in rows])
    lm, ls = np.median([lum(r).mean() for r in rows]), np.std([lum(r).mean() for r in rows]) + 1e-6
    rows = [r for r in rows if abs(r.shape[1] - wm) < wm * .06 and abs(lum(r).mean() - lm) < 1.6 * ls] or rows
    if n_rows:
        rows = rows[:n_rows]
    Wt, Hr = int(np.median([r.shape[1] for r in rows])), int(np.median([r.shape[0] for r in rows]))
    out = [np.asarray(Image.fromarray(r.astype(np.uint8)).resize((Wt, Hr), Image.LANCZOS)) for r in rows]
    rng = np.random.RandomState(len(out))
    out = [np.roll(r, int(rng.uniform(0, Wt)), 1) for r in out]
    print(f"  错缝：行高 {Hr}px × {len(out)} 行，宽 {Wt}px")
    return Image.fromarray(np.concatenate(out, 0))


def grid(src):
    a = np.asarray(src, np.float32)
    L = lum(a)
    py, px = period(L.mean(1), 20, 300), period(L.mean(0), 20, 300)
    ys, xs = joints(L.mean(1), py), joints(L.mean(0), px)
    print(f"  方砖：{len(xs) - 1}×{len(ys) - 1} 块，周期 {px}×{py}px")
    return Image.fromarray(a[ys[0]:ys[-1], xs[0]:xs[-1]].astype(np.uint8))


def soft(src):
    a = np.asarray(src, np.float32)
    h, w = a.shape[:2]
    r = np.roll(np.roll(a, h // 2, 0), w // 2, 1)
    m = ((np.sin(np.pi * (np.arange(h) + .5) / h) ** 2)[:, None] * (np.sin(np.pi * (np.arange(w) + .5) / w) ** 2)[None, :])
    out = a * m[..., None] + r * (1 - m[..., None])
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(.6))


def wrapx(src):
    a = np.asarray(src, np.float32)
    w = a.shape[1]
    r = np.roll(a, w // 2, 1)
    m = (np.sin(np.pi * (np.arange(w) + .5) / w) ** 2)[None, :, None]
    return Image.fromarray(np.clip(a * m + r * (1 - m), 0, 255).astype(np.uint8))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--mode", choices=["bond", "grid", "soft", "wrapx"], required=True)
    ap.add_argument("--crop", help="x0,y0,x1,y1 比例，先裁掉边上的杂色带")
    ap.add_argument("--rows", type=int)
    ap.add_argument("--check", help="另存一张 2×2 平铺的检查图")
    a = ap.parse_args()
    im = Image.open(a.src).convert("RGB")
    if a.crop:
        c = [float(v) for v in a.crop.split(",")]
        im = im.crop((int(c[0] * im.width), int(c[1] * im.height), int(c[2] * im.width), int(c[3] * im.height)))
    t = {"bond": lambda s: running_bond(s, a.rows), "grid": grid, "soft": soft, "wrapx": wrapx}[a.mode](im)
    t.save(a.dst)
    print(f"-> {a.dst} {t.size}")
    if a.check:
        c = Image.new("RGB", (t.width * 2, t.height * 2))
        for i in range(4):
            c.paste(t, ((i % 2) * t.width, (i // 2) * t.height))
        c.thumbnail((1800, 1800))
        c.save(a.check, quality=88)
        print(f"   检查图 {a.check}")


if __name__ == "__main__":
    main()
