"""从成片（或任意视频）按秒数抽帧，拼成带时间标注的网格图，长边自动控制在 1900 像素以内
（看图工具：一次请求放多张图时任意一边不能超过 2000 像素，单张不能超过 8000 像素、5MB）。

用法:
  python frame_grid.py film.mp4 10 25 45 65 84 100 --cols 2 --out /tmp/grid.jpg
  python frame_grid.py film.mp4 76:84:1 --cols 3 --crop 1150,300,1650,680 --out /tmp/strip.jpg   # a:b:步长；先裁再拼
"""
import argparse
import subprocess

import numpy as np
from PIL import Image, ImageDraw


def ffmpeg():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        return "ffmpeg"


def grab(video, t):
    raw = subprocess.run([ffmpeg(), "-v", "error", "-ss", f"{t:.3f}", "-i", video, "-frames:v", "1", "-f", "image2pipe",
                          "-vcodec", "png", "-"], capture_output=True, check=True).stdout
    import io
    return Image.open(io.BytesIO(raw)).convert("RGB")


def parse_times(tokens):
    out = []
    for tok in tokens:
        if ":" in tok:
            a, b, s = (float(v) for v in tok.split(":"))
            out += list(np.arange(a, b + 1e-9, s))
        else:
            out.append(float(tok))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("times", nargs="+")
    ap.add_argument("--cols", type=int, default=2)
    ap.add_argument("--crop", help="x0,y0,x1,y1（原尺寸像素）")
    ap.add_argument("--max", type=int, default=1900, help="拼图长边上限")
    ap.add_argument("--out", default="/tmp/frame_grid.jpg")
    a = ap.parse_args()
    ts = parse_times(a.times)
    tiles = []
    for t in ts:
        im = grab(a.video, t)
        if a.crop:
            im = im.crop(tuple(int(v) for v in a.crop.split(",")))
        tiles.append((t, im))
    cols = min(a.cols, len(tiles))
    rows = (len(tiles) + cols - 1) // cols
    w0, h0 = tiles[0][1].size
    k = min(1.0, a.max / (w0 * cols), a.max / (h0 * rows))
    tw, th = int(w0 * k), int(h0 * k)
    out = Image.new("RGB", (tw * cols, th * rows), "white")
    d = ImageDraw.Draw(out)
    for i, (t, im) in enumerate(tiles):
        x, y = (i % cols) * tw, (i // cols) * th
        out.paste(im.resize((tw, th), Image.LANCZOS), (x, y))
        d.rectangle([x, y, x + 56, y + 16], fill=(0, 0, 0))
        d.text((x + 4, y + 3), f"{t:6.2f}s", fill=(255, 255, 255))
    out.save(a.out, quality=88)
    print(a.out, out.size)


if __name__ == "__main__":
    main()
