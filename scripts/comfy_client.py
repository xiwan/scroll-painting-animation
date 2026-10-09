"""ComfyUI 精灵生产的最小客户端（只用标准库 + Pillow）：提交工作流、等队列、按前缀拉回输出、
把"原图 + 抠图蒙版"合成带透明的精灵（去碎块、裁边、缩到目标尺寸）。

内置一个"文生图 + BiRefNet 抠图"的 API 格式工作流模板（Z-Image Turbo 9 步），模型文件名可用参数换；
也可以 --workflow 传任何 API 格式的 JSON，用 --set 节点.inputs.键=值 改参数。

用法（--host 默认 http://127.0.0.1:18188，即 infra.md 里 SSM 隧道的本机端口；ComfyUI 就在本机时用 :8188）:
  python comfy_client.py t2i --name bld_teahouse --w 1024 --h 1024 --seed 7 --prompt "..."     # 排队
  python comfy_client.py wait                                                                # 等队列清空
  python comfy_client.py sprite --name bld_teahouse --out cut/ --max 720                     # 拉回 RGB+蒙版 → 透明精灵
  python comfy_client.py run --workflow my_api.json --set 6.inputs.text="a cat" --set 3.inputs.seed=5
  python comfy_client.py upload some.png [--sub sfx]                                         # 传到 input/
  python comfy_client.py free                                                                # 卸载模型、释放显存

注意：下行带宽很低时（跨洋），把合成/裁边/压 WebP 放到服务器上做再拉小文件更快——见 references/assets.md。
"""
import argparse
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import uuid

from PIL import Image, ImageFilter

Image.MAX_IMAGE_PIXELS = 80_000_000          # 挡住恶意超大图（解压炸弹）打爆内存
_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


def safe_name(v):
    """--name 会进本地文件路径和服务端 filename_prefix，不许路径穿越。"""
    if not v or not _NAME_RE.match(v):
        raise SystemExit(f"--name 非法：{v!r}（只允许字母数字 _ - ，1–64 字符）")
    return v


def safe_host(v):
    """只允许 http/https；默认只允许回环地址——upload 会把本地文件发给它，跨机须显式放开。"""
    u = urllib.parse.urlparse(v)
    if u.scheme not in ("http", "https"):
        raise SystemExit(f"--host 只允许 http/https：{v!r}")
    host = (u.hostname or "").lower()
    local = host in ("127.0.0.1", "::1", "localhost") or host.startswith("127.")
    if not local and os.environ.get("COMFY_ALLOW_REMOTE_HOST") != "1":
        raise SystemExit(f"--host 指向非本机地址 {host}；本地文件会被上传到该地址。确认无误后设 "
                         "COMFY_ALLOW_REMOTE_HOST=1（建议改用 SSM/SSH 端口转发到 127.0.0.1）")
    return v.rstrip("/")


def safe_out(out, name):
    """输出必须落在 --out 目录内。"""
    d = os.path.realpath(out)
    p = os.path.realpath(os.path.join(d, f"{name}.png"))
    if os.path.dirname(p) != d:
        raise SystemExit(f"拒绝写到 --out 之外：{p}")
    return p


def _mp_name(path):
    """multipart 头里的文件名：去掉引号与换行，防 header 注入。"""
    n = os.path.basename(path)
    return n.replace("\\", "").replace('"', "").replace("\r", "").replace("\n", "")


def post(host, path, obj):
    req = urllib.request.Request(host + path, data=json.dumps(obj).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        body = r.read()
    return json.loads(body) if body else {}


def get(host, path):
    with urllib.request.urlopen(host + path, timeout=300) as r:
        return r.read()


def queue_left(host):
    q = json.loads(get(host, "/queue"))
    return len(q.get("queue_running", [])) + len(q.get("queue_pending", []))


def view(host, filename, sub="", typ="output"):
    q = urllib.parse.urlencode({"filename": filename, "subfolder": sub, "type": typ})
    return get(host, f"/view?{q}")


def upload(host, path, sub="", overwrite=True):
    """multipart 上传到 ComfyUI 的 input/（图片、视频片段都行）。返回服务器上的相对名。"""
    bnd = uuid.uuid4().hex
    parts = [f'--{bnd}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
             for k, v in (("subfolder", sub), ("type", "input"), ("overwrite", "true" if overwrite else "false"))]
    parts.append(f'--{bnd}\r\nContent-Disposition: form-data; name="image"; filename="{_mp_name(path)}"\r\n'
                 f'Content-Type: application/octet-stream\r\n\r\n'.encode() + open(path, "rb").read() + b"\r\n")
    parts.append(f"--{bnd}--\r\n".encode())
    req = urllib.request.Request(host + "/upload/image", data=b"".join(parts),
                                 headers={"Content-Type": f"multipart/form-data; boundary={bnd}"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())


def t2i_workflow(prompt, w, h, seed, stem, unet="z_image_turbo_bf16.safetensors", clip="qwen_3_4b.safetensors",
                 clip_type="lumina2", vae="ae.safetensors", bg="birefnet.safetensors", steps=9):
    """文生图（Z-Image Turbo，无负向提示）+ 同一张图的 BiRefNet 抠图蒙版。输出 gen/<stem>_*.png 与 genmask/<stem>_*.png。
    Z-Image 没有负向提示：正向里写 "no house" 反而招来房子——人物/道具的提示词里不要出现建筑词汇。"""
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": unet, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": clip, "type": clip_type, "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": vae}},
        "4": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["1", 0], "shift": 3.0}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["2", 0]}},
        "6": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["5", 0]}},
        "7": {"class_type": "EmptySD3LatentImage", "inputs": {"width": w, "height": h, "batch_size": 1}},
        "8": {"class_type": "KSampler", "inputs": {"model": ["4", 0], "positive": ["5", 0], "negative": ["6", 0],
                                                   "latent_image": ["7", 0], "seed": seed, "steps": steps, "cfg": 1.0,
                                                   "sampler_name": "res_multistep", "scheduler": "simple", "denoise": 1.0}},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
        "10": {"class_type": "SaveImage", "inputs": {"images": ["9", 0], "filename_prefix": f"gen/{stem}"}},
        "11": {"class_type": "LoadBackgroundRemovalModel", "inputs": {"bg_removal_name": bg}},
        "12": {"class_type": "RemoveBackground", "inputs": {"bg_removal_model": ["11", 0], "image": ["9", 0]}},
        "13": {"class_type": "MaskToImage", "inputs": {"mask": ["12", 0]}},
        "14": {"class_type": "SaveImage", "inputs": {"images": ["13", 0], "filename_prefix": f"genmask/{stem}"}},
    }


def latest_output(host, prefix):
    """在历史记录里找最近一次以 prefix（如 gen/bld_x）存下的图，返回 (filename, subfolder)。"""
    hist = json.loads(get(host, "/history?max_items=400"))
    best = None
    for pid, h in hist.items():
        for node in h.get("outputs", {}).values():
            for im in node.get("images", []):
                full = (im.get("subfolder", "") + "/" if im.get("subfolder") else "") + im["filename"]
                if full.startswith(prefix + "_"):
                    if best is None or full > best[2]:
                        best = (im["filename"], im.get("subfolder", ""), full)
    return best[:2] if best else None


def compose_sprite(rgb, mask, max_side=None, height=None, min_frac=.05, erode=3):
    """原图 + 抠图蒙版 → 透明精灵：蒙版收一圈去毛边、拉对比；去掉面积不到最大块 min_frac 的碎块；裁到不透明区域；
    按最长边 max_side 或高度 height 缩小。图集（一行四个人）可另按连通块拆，见 references/assets.md。"""
    import numpy as np
    a = mask.convert("L").resize(rgb.size)
    a = np.asarray(a.filter(ImageFilter.MinFilter(erode)), np.float32)
    a = np.clip((a - 20) * 255 / 215, 0, 255).astype(np.uint8)
    m = a > 40
    # 去碎块：用简单的洪泛标记连通块（小图上够快）
    lab = np.zeros(m.shape, np.int32)
    sizes, cur = [0], 0
    H, W = m.shape
    for y0, x0 in zip(*np.nonzero(m)):
        if lab[y0, x0]:
            continue
        cur += 1
        stack, n = [(y0, x0)], 0
        lab[y0, x0] = cur
        while stack:
            y, x = stack.pop()
            n += 1
            for yy, xx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                if 0 <= yy < H and 0 <= xx < W and m[yy, xx] and not lab[yy, xx]:
                    lab[yy, xx] = cur
                    stack.append((yy, xx))
        sizes.append(n)
    if cur > 1:
        big = max(sizes)
        keep = np.isin(lab, [i for i, s in enumerate(sizes) if i and s >= big * min_frac])
        a = np.where(keep, a, 0).astype(np.uint8)
    im = rgb.convert("RGB").copy()
    im.putalpha(Image.fromarray(a))
    bb = im.getchannel("A").point(lambda v: 255 if v > 10 else 0).getbbox()
    if bb:
        im = im.crop((max(0, bb[0] - 4), max(0, bb[1] - 4), min(im.width, bb[2] + 4), min(im.height, bb[3] + 4)))
    k = 1.0
    if max_side:
        k = min(1.0, max_side / max(im.size))
    if height:
        k = min(1.0, height / im.height)
    if k < 1:
        im = im.resize((max(1, round(im.width * k)), max(1, round(im.height * k))), Image.LANCZOS)
    return im


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["t2i", "run", "wait", "sprite", "upload", "free"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--host", default=os.environ.get("COMFY_HOST", "http://127.0.0.1:18188"),
                    help="默认 http://127.0.0.1:18188（infra.md 里 SSM 隧道的本机端口）；"
                         "ComfyUI 就在本机时用 --host http://127.0.0.1:8188")
    ap.add_argument("--name")
    ap.add_argument("--prompt")
    ap.add_argument("--w", type=int, default=1024)
    ap.add_argument("--h", type=int, default=1024)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--workflow")
    ap.add_argument("--set", action="append", default=[])
    ap.add_argument("--out", default=".")
    ap.add_argument("--max", type=int)
    ap.add_argument("--height", type=int)
    ap.add_argument("--sub", default="")
    a = ap.parse_args()
    a.host = safe_host(a.host)
    if a.cmd in ("t2i", "sprite"):
        safe_name(a.name)
    if a.cmd == "t2i":
        print(post(a.host, "/prompt", {"prompt": t2i_workflow(a.prompt, a.w, a.h, a.seed, a.name)}))
    elif a.cmd == "run":
        # workflow JSON 等同代码：ComfyUI 的 /prompt 默认无鉴权，工作流能加载任意权重和自定义节点。
        print("[提醒] 只跑自己写的或审阅过的 workflow JSON", file=sys.stderr)
        wf = json.load(open(a.workflow))
        for s in a.set:
            path, val = s.split("=", 1)
            keys = path.split(".")
            node = wf
            for k_ in keys[:-1]:
                node = node[k_]
            try:
                val = json.loads(val)
            except json.JSONDecodeError:
                pass
            node[keys[-1]] = val
        print(post(a.host, "/prompt", {"prompt": wf}))
    elif a.cmd == "wait":
        while (n := queue_left(a.host)):
            print(f"  队列剩余 {n}", flush=True)
            time.sleep(10)
        print("队列空")
    elif a.cmd == "sprite":
        r, m = latest_output(a.host, f"gen/{a.name}"), latest_output(a.host, f"genmask/{a.name}")
        if not r or not m:
            raise SystemExit(f"历史里找不到 {a.name} 的输出")
        rgb = Image.open(io.BytesIO(view(a.host, r[0], r[1])))
        mask = Image.open(io.BytesIO(view(a.host, m[0], m[1])))
        im = compose_sprite(rgb, mask, a.max, a.height)
        os.makedirs(a.out, exist_ok=True)
        p = safe_out(a.out, a.name)
        im.save(p)
        print(p, im.size)
    elif a.cmd == "upload":
        for f in a.args:
            print(upload(a.host, f, a.sub))
    elif a.cmd == "free":
        print(post(a.host, "/free", {"unload_models": True, "free_memory": True}))


if __name__ == "__main__":
    main()
