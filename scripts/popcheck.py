"""逐帧跳变检查：把成片解码成小尺寸灰度，每帧与上一帧比较（补偿镜头平移 shifts 像素），找出"突然一跳"的帧
（精灵换图跳位、图层闪烁、东西突然出现/消失）。

关键：编码器每 GOP（x264 默认 250 帧）插一个关键帧，那一帧的差值会略高——这是压缩，不是画面问题，默认跳过；
首尾淡入淡出也跳过。

用法:
  python popcheck.py film.mp4                     # 默认 960×540、镜头每帧约 1~3 像素（960 宽下）
  python popcheck.py film.mp4 --shifts 0 1 2 3 4 --z 8 --gop 250 --skip 60
"""
import argparse
import subprocess

import numpy as np


def ffmpeg():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        return "ffmpeg"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--w", type=int, default=960)
    ap.add_argument("--h", type=int, default=540)
    ap.add_argument("--shifts", type=int, nargs="+", default=[1, 2, 3], help="镜头每帧水平平移（像素，小图尺度）")
    ap.add_argument("--z", type=float, default=8.0, help="离群阈值（中位数 + z×MAD）")
    ap.add_argument("--gop", type=int, default=250, help="关键帧间隔，0 = 不跳过")
    ap.add_argument("--skip", type=int, default=60, help="首尾各跳过多少帧（淡入淡出）")
    ap.add_argument("--fps", type=float, default=30.0)
    a = ap.parse_args()
    W, H = a.w, a.h
    p = subprocess.Popen([ffmpeg(), "-v", "error", "-i", a.video, "-vf", f"scale={W}:{H}", "-f", "rawvideo",
                          "-pix_fmt", "gray", "-"], stdout=subprocess.PIPE)
    prev, md = None, []

    while True:
        buf = p.stdout.read(W * H)
        if len(buf) < W * H:
            break
        f = np.frombuffer(buf, np.uint8).reshape(H, W).astype(np.int16)
        if prev is not None:
            md.append(min(float(np.abs(f[30:-30, 40:-40] - prev[30:-30, 40 + s:W - 40 + s]).mean())
                          for s in a.shifts))
        prev = f
    p.wait()
    md = np.array(md)
    if not len(md):
        raise SystemExit("没读到帧")
    med = np.median(md)
    mad = np.median(np.abs(md - med)) + 1e-6
    z = (md - med) / mad
    out = [k for k in np.where(z > a.z)[0]
           if a.skip < k < len(md) - a.skip and not (a.gop and (k + 1) % a.gop == 0)]
    print(f"帧数 {len(md) + 1}  中位差 {med:.2f}  离群 {len(out)}")
    for k in out[:40]:
        print(f"  第 {k + 1} 帧 t={(k + 1) / a.fps:.2f}s  差 {md[k]:.2f}  z {z[k]:.1f}")
    step = int(a.fps * 20)
    for s in range(0, len(md), step):
        print(f"  {s / a.fps:5.0f}-{(s + step) / a.fps:4.0f}s  均值 {md[s:s + step].mean():.2f}  最大 {md[s:s + step].max():.2f}")


if __name__ == "__main__":
    main()
