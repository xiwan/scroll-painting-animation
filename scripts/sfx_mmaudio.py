"""按画面配音效：把成片切成重叠的时间窗，用 MMAudio（视频 + 文字条件）在 GPU 机上生成环境声；再生成几段"场景音效"
（孩童嬉戏、城门开启、钟声……）叠上去；用 AudioSet 分类模型（AST）给每段打标签核对、对不上的多出候选自动挑；
最后等功率交叉淡化拼接、分段增益、限幅，并入视频。一切由一个 JSON 配置驱动（样例见 assets/sfx_config.example.json）。

传输/执行两种方式（配置 remote 段）：
  comfy：GPU 机上跑着 ComfyUI——片段经 /upload/image 传上去，结果经 /view 拉回；命令经 runner 执行（如 ssm_run.sh / ssh）
  local：本机就是 GPU 机（runner 为 null、comfy 为 null）——直接读写本地路径

用法:
  python sfx_mmaudio.py CONFIG plan            # 打印时间窗与提示词，先核对再花 GPU
  python sfx_mmaudio.py CONFIG cut             # 切片段（窗口 + 场景音效）并上传
  python sfx_mmaudio.py CONFIG run [w03 w10]   # 生成环境声（全部或指定窗口）
  python sfx_mmaudio.py CONFIG tag             # AST 打标签，列出疑似"配乐/雷声"等不对的窗口
  python sfx_mmaudio.py CONFIG search w10 w15  # 对不上的窗口：多出候选（一半带画面、一半纯文字），打分挑最好
  python sfx_mmaudio.py CONFIG events [名..]   # 场景音效：每条多出候选、打分挑最好
  python sfx_mmaudio.py CONFIG fetch           # 拉回环境声窗口
  python sfx_mmaudio.py CONFIG mix             # 混音并入视频（写 output_video，不动原片）
  python sfx_mmaudio.py CONFIG auto            # cut → run → tag → search(自动挑出的窗口) → events → fetch → mix

GPU 机上的 MMAudio 安装见 references/audio.md。权重许可 CC BY-NC 4.0（非商用）。
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import numpy as np

# ---- 配置校验：配置文件里的路径/名字会进 shell 命令和文件路径，先卡住再用 ----
_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,48}\Z")          # 窗口名、场景音效名
_RPATH_RE = re.compile(r"/[A-Za-z0-9._+@/-]*\Z")         # 远端绝对路径：不含空格与 shell 元字符


def _safe_name(v, what):
    if not isinstance(v, str) or not _NAME_RE.match(v):
        raise SystemExit(f"配置里的 {what} 非法：{v!r}（只允许字母数字 _ - ，1–48 字符）")
    return v


def _safe_rpath(v, what):
    """远端路径：必须是绝对路径、不含 .. 、不含空格和 shell 元字符，且不是 / 或一级目录。"""
    if not isinstance(v, str) or not _RPATH_RE.match(v) or ".." in v.split("/"):
        raise SystemExit(f"配置里的 {what} 非法：{v!r}（需绝对路径，只允许 A-Za-z0-9 . _ + @ - / ）")
    if len([p for p in v.split("/") if p]) < 2:
        raise SystemExit(f"配置里的 {what} 层级太浅：{v!r}（至少两级，避免误删）")
    return v.rstrip("/")


def _safe_url(v, what, allow_remote=False):
    """只允许 http/https；默认只允许回环地址，跨机访问必须显式放开（防 SSRF 与素材外传）。"""
    u = urllib.parse.urlparse(v)
    if u.scheme not in ("http", "https"):
        raise SystemExit(f"配置里的 {what} 只允许 http/https：{v!r}")
    host = (u.hostname or "").lower()
    local = host in ("127.0.0.1", "::1", "localhost") or host.startswith("127.")
    if not local and not allow_remote:
        raise SystemExit(
            f"{what} 指向非本机地址 {host}；片段会被上传到该地址。确认无误后设环境变量 "
            f"SFX_ALLOW_REMOTE_HOST=1 再跑（建议改用 SSM/SSH 端口转发到 127.0.0.1）")
    return v.rstrip("/")


_SECRET_RE = re.compile(r"(?i)\b(AKIA[0-9A-Z]{16}|(?:token|secret|password|passwd|api[_-]?key)\s*[=:]\s*\S+)")


def _redact(s):
    """远端日志可能带环境变量/凭据，回显前脱敏（回显会进终端，也会进 agent 上下文）。"""
    return _SECRET_RE.sub("***", s)


def _noopt(p):
    """给 ffmpeg 的路径加 ./ 前缀，防止以 - 开头的文件名被当成选项。"""
    p = str(p)
    return p if os.path.isabs(p) or p.startswith("./") else "./" + p


MUSIC = ["Music", "Theme music", "Background music", "Musical instrument", "Singing", "Orchestra", "Soundtrack music",
         "Sad music", "Scary music", "Wedding music", "Marimba, xylophone", "Glockenspiel", "Mallet percussion",
         "Thunder", "Thunderstorm", "Speech synthesizer"]
BELLISH = {"Tubular bells", "Glockenspiel", "Mallet percussion", "Marimba, xylophone"}

SERVER = r'''
import json, os, sys, torch, torchaudio
from pathlib import Path
MM = sys.argv[1]; JOBS = sys.argv[2]; OUT = Path(sys.argv[3])
os.chdir(MM); sys.path.insert(0, MM)
from mmaudio.eval_utils import all_model_cfg, generate, load_video
from mmaudio.model.flow_matching import FlowMatching
from mmaudio.model.networks import get_my_mmaudio
from mmaudio.model.utils.features_utils import FeaturesUtils
torch.backends.cuda.matmul.allow_tf32 = True
model = all_model_cfg["large_44k_v2"]; model.download_if_needed(); seq = model.seq_cfg
dev, dt = "cuda", torch.bfloat16
net = get_my_mmaudio(model.model_name).to(dev, dt).eval()
net.load_weights(torch.load(model.model_path, map_location=dev, weights_only=True))
fu = FeaturesUtils(tod_vae_ckpt=model.vae_path, synchformer_ckpt=model.synchformer_ckpt, enable_conditions=True,
                   mode=model.mode, bigvgan_vocoder_ckpt=model.bigvgan_16k_path, need_vae_encoder=False).to(dev, dt).eval()
with torch.inference_mode():
    for j in json.load(open(JOBS)):
        rng = torch.Generator(device=dev); rng.manual_seed(j["seed"])
        fm = FlowMatching(min_sigma=0, inference_mode="euler", num_steps=j["steps"])
        if j.get("video"):
            vi = load_video(Path(j["video"]), j["dur"])
            clip, sync, dur = vi.clip_frames.unsqueeze(0), vi.sync_frames.unsqueeze(0), vi.duration_sec
        else:
            clip = sync = None; dur = j["dur"]
        seq.duration = dur
        net.update_seq_lengths(seq.latent_seq_len, seq.clip_seq_len, seq.sync_seq_len)
        a = generate(clip, sync, [j["prompt"]], negative_text=[j["neg"]], feature_utils=fu, net=net, fm=fm, rng=rng,
                     cfg_strength=j["cfg"])
        o = OUT / j.get("sub", ""); o.mkdir(parents=True, exist_ok=True)
        torchaudio.save(str(o / (j["name"] + ".flac")), a.float().cpu()[0], seq.sampling_rate)
        print("done", j["name"], round(dur, 2), flush=True)
print("ALL_DONE", flush=True)
'''

TAGGER = r'''
import glob, json, os, sys, av, numpy as np, torch
from transformers import ASTFeatureExtractor, ASTForAudioClassification
name = "MIT/ast-finetuned-audioset-10-10-0.4593"
fe, m = ASTFeatureExtractor.from_pretrained(name), ASTForAudioClassification.from_pretrained(name).eval()
def load(f):
    c = av.open(f); rs = av.AudioResampler(format="flt", layout="mono", rate=16000)
    return np.concatenate([fr.to_ndarray().reshape(-1) for p in c.decode(audio=0) for fr in rs.resample(p)]).astype(np.float32)
res = {}
for f in sorted(glob.glob(sys.argv[1])):
    with torch.no_grad():
        p = torch.sigmoid(m(**fe(load(f), sampling_rate=16000, return_tensors="pt")).logits[0])
    top = torch.topk(p, 20)
    res[os.path.basename(f)[:-5]] = {m.config.id2label[int(i)]: round(float(v), 3) for v, i in zip(*top)}
print("JSON" + json.dumps(res))
'''


class Cfg:
    def __init__(self, path):
        c = json.load(open(path))
        base = Path(path).resolve().parent
        self.video = (base / c["video"]).resolve()
        self.out_video = (base / c.get("output_video", Path(c["video"]).stem + "_sfx.mp4")).resolve()
        self.dur, self.win, self.step, self.sr = c["duration"], c.get("win", 10.0), c.get("step", 8.0), c.get("sr", 44100)
        self.neg = c.get("negative", "music, singing, modern vehicles, car engine, electronic sounds")
        self.scenes, self.fix, self.want = c["scenes"], c.get("fix", {}), c.get("want", {})
        self.events = c.get("events", [])
        for e in self.events:                      # 名字会进文件路径和 shell，先校验
            _safe_name(e[0], "events 里的名字")
        self.out = (base / c.get("out_dir", "sfx")).resolve()
        self.work = (base / c.get("work_dir", "sfx_work")).resolve()
        self.rms_db, self.limit = c.get("target_rms_db", -21.0), c.get("limit", .7)
        r = c.get("remote", {})
        self.comfy = r.get("comfy")                 # 例 "http://127.0.0.1:18188"；null = 本机路径
        if self.comfy:
            self.comfy = _safe_url(self.comfy, "remote.comfy",
                                   allow_remote=os.environ.get("SFX_ALLOW_REMOTE_HOST") == "1")
        self.comfy_dir = _safe_rpath(r.get("comfy_dir", "/opt/ComfyUI"), "remote.comfy_dir")
        self.mm_dir = _safe_rpath(r.get("mmaudio_dir", "/opt/mmaudio/src"), "remote.mmaudio_dir")
        self.mm_py = _safe_rpath(r.get("mmaudio_python", "/opt/mmaudio/venv/bin/python"), "remote.mmaudio_python")
        self.tag_py = _safe_rpath(r.get("tagger_python", "/opt/ComfyUI/venv/bin/python"), "remote.tagger_python")
        self.remote_work = _safe_rpath(r.get("remote_work", "/opt/mmaudio/work"), "remote.remote_work")
        # runner[0] 是会被执行的程序：配置等同代码，这里只做存在性检查并回显，别跑来源不明的配置
        self.runner = r.get("runner")
        if self.runner:
            if not isinstance(self.runner, list) or not self.runner or not all(isinstance(x, str) for x in self.runner):
                raise SystemExit("remote.runner 必须是字符串数组，如 [\"/path/ssm_run.sh\", \"{cmd}\", \"{timeout}\"]")
            for x in self.runner:                 # 顺手挡住占位路径/拼错路径
                if x.startswith("/") and not Path(x).is_file():
                    raise SystemExit(f"remote.runner 里的路径不存在：{x}"
                                     "（样例配置里是占位符，请改成你机器上 ssm_run.sh / ssh 的真实路径）")
        self._runner_shown = False

    # ---- 远端执行与传输
    def sh(self, cmd, timeout=600):
        if not self.runner:
            return subprocess.run(["bash", "-c", cmd], check=True, capture_output=True, text=True, timeout=timeout).stdout
        if not self._runner_shown:
            print("[远端执行器]", " ".join(self.runner), file=sys.stderr)
            self._runner_shown = True
        argv = [a.replace("{cmd}", cmd).replace("{timeout}", str(timeout)) for a in self.runner]
        return subprocess.run(argv, check=True, capture_output=True, text=True, timeout=timeout + 120).stdout

    def in_path(self, name):
        return f"{self.comfy_dir}/input/sfx/{name}" if self.comfy else str(self.work / "in" / name)

    def out_root(self):
        return f"{self.comfy_dir}/output/sfx" if self.comfy else str(self.work / "out")

    def put(self, path):
        if not self.comfy:
            (self.work / "in").mkdir(parents=True, exist_ok=True)
            shutil.copy(path, self.work / "in" / Path(path).name)
            return
        bnd = uuid.uuid4().hex
        fn = Path(path).name.replace("\\", "").replace('"', "").replace("\r", "").replace("\n", "")  # 防 multipart 头注入
        parts = [f'--{bnd}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
                 for k, v in (("subfolder", "sfx"), ("type", "input"), ("overwrite", "true"))]
        parts.append(f'--{bnd}\r\nContent-Disposition: form-data; name="image"; filename="{fn}"\r\n'
                     f'Content-Type: video/mp4\r\n\r\n'.encode() + Path(path).read_bytes() + b"\r\n")
        parts.append(f"--{bnd}--\r\n".encode())
        req = urllib.request.Request(f"{self.comfy}/upload/image", data=b"".join(parts),
                                     headers={"Content-Type": f"multipart/form-data; boundary={bnd}"})
        urllib.request.urlopen(req, timeout=300).read()

    def get(self, rel, dst):
        """rel：相对远端输出根目录的路径（如 w03.flac、evc/ev_bell_c2.flac）。dst 必须落在 out_dir 内。"""
        dst = Path(dst).resolve()
        if self.out not in dst.parents and dst.parent != self.out:
            raise SystemExit(f"拒绝写到 out_dir 之外：{dst}")
        if ".." in Path(rel).parts:
            raise SystemExit(f"远端路径非法：{rel!r}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not self.comfy:
            shutil.copy(Path(self.out_root()) / rel, dst)
            return
        sub, fn = os.path.split(rel)
        q = urllib.parse.urlencode({"filename": fn, "subfolder": "sfx/" + sub if sub else "sfx", "type": "output"})
        with urllib.request.urlopen(f"{self.comfy}/view?{q}", timeout=300) as r:
            dst.write_bytes(r.read())

    def free_comfy_vram(self):
        if self.comfy:            # ComfyUI 常驻会占着显存；跑 MMAudio 前先让它卸载模型
            port = urllib.parse.urlparse(self.comfy).port or 8188
            self.sh("curl -s -X POST -H 'Content-Type: application/json' "
                    f"-d '{{\"unload_models\":true,\"free_memory\":true}}' http://127.0.0.1:{int(port)}/free || true", 60)

    def batch(self, jobs, clear_sub=None):
        self.free_comfy_vram()
        rw, root = self.remote_work, self.out_root()
        clear = f"rm -rf {shlex.quote(root + '/' + _safe_name(clear_sub, 'clear_sub'))}; " if clear_sub else ""
        cmd = (f"set -u; mkdir -p {shlex.quote(rw)} {shlex.quote(root)}; {clear}"
               f"cat > {shlex.quote(rw + '/jobs.json')} <<'JEOF'\n{json.dumps(jobs, ensure_ascii=False)}\nJEOF\n"
               f"cat > {shlex.quote(rw + '/batch.py')} <<'PYEOF'\n{SERVER}\nPYEOF\n"
               f"{shlex.quote(self.mm_py)} {shlex.quote(rw + '/batch.py')} {shlex.quote(self.mm_dir)} "
               f"{shlex.quote(rw + '/jobs.json')} {shlex.quote(root)} > {shlex.quote(rw + '/batch.log')} 2>&1; "
               f"tail -3 {shlex.quote(rw + '/batch.log')}")
        out = self.sh(cmd, 3600)
        if "ALL_DONE" not in out:
            raise SystemExit("MMAudio 批处理失败（远端日志末尾，可能含路径信息）：\n" + _redact(out))
        return out

    def tags(self, pattern):
        rw = self.remote_work
        cmd = (f"set -u; mkdir -p {shlex.quote(rw)}; cat > {shlex.quote(rw + '/tag.py')} <<'PYEOF'\n{TAGGER}\nPYEOF\n"
               f"{shlex.quote(self.tag_py)} {shlex.quote(rw + '/tag.py')} "
               f"{shlex.quote(self.out_root() + '/' + pattern)} 2>/dev/null")
        out = self.sh(cmd, 1800)
        return json.loads(out[out.index("JSON") + 4:].splitlines()[0])


    # ---- 时间窗
    def scene_at(self, t):
        for a, b, p, g in self.scenes:
            if a <= t < b:
                return p, g
        return self.scenes[-1][2], self.scenes[-1][3]

    def windows(self):
        out, s, i = [], 0.0, 0
        while s < self.dur:
            p, g = self.scene_at(s + self.win / 2)
            p, seed = self.fix.get(f"w{i:02d}", (p, 0))
            out.append(dict(name=f"w{i:02d}", i=i, t0=s, prompt=p, gain=g, seed=seed))
            s += self.step
            i += 1
        return out


def ffmpeg():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        return "ffmpeg"


def cut_clip(c, t0, dur, dst):
    subprocess.run([ffmpeg(), "-y", "-loglevel", "error", "-ss", f"{t0:.3f}", "-t", f"{dur:.3f}", "-i", _noopt(c.video),
                    "-vf", "scale=640:360", "-an", "-c:v", "libx264", "-crf", "20", "-preset", "fast", str(dst)], check=True)


def score(tag, want, pen):
    return sum(tag.get(a, 0) for a in want) - 2.0 * sum(tag.get(a, 0) for a in pen)


def cmd_cut(c):
    c.work.mkdir(parents=True, exist_ok=True)
    for w in c.windows():
        dst = c.work / f"{w['name']}.mp4"
        cut_clip(c, w["t0"], min(c.win, c.dur - w["t0"]), dst)
        c.put(dst)
    for name, t0, dur, *_ in c.events:
        dst = c.work / f"ev_{name}.mp4"
        cut_clip(c, t0, min(dur, c.dur - t0), dst)
        c.put(dst)
    print("已切片上传：", len(c.windows()), "个窗口，", len(c.events), "段场景音效")


def cmd_run(c, names=None):
    jobs = [dict(name=w["name"], video=c.in_path(w["name"] + ".mp4"), dur=min(c.win, c.dur - w["t0"]),
                 prompt=w["prompt"], neg=c.neg,
                 seed=20260930 + w["i"] + 1000 * w["seed"], steps=30, cfg=5.5 if w["gain"] >= 3 else 4.5)
            for w in c.windows() if not names or w["name"] in names]
    print(c.batch(jobs).strip().splitlines()[-1])


def suspicious(c, tags):
    bad = []
    for w in c.windows():
        t = tags.get(w["name"], {})
        if sum(t.get(a, 0) for a in MUSIC) > .15 or max(t.values() or [0]) < .1:
            bad.append(w["name"])
    return bad


def cmd_tag(c):
    tags = c.tags("w*.flac")
    for k, v in tags.items():
        print(k, " | ".join(f"{a} {b:.2f}" for a, b in list(v.items())[:6]))
    bad = suspicious(c, tags)
    print("疑似不对（配乐/雷声/几乎无声）：", bad)
    return bad


def cmd_search(c, names, k=6):
    wins = {w["name"]: w for w in c.windows()}
    jobs = []
    for nm in names:
        w = wins[nm]
        for j in range(k):
            jobs.append(dict(name=f"{nm}_c{j}", sub="cand", video=c.in_path(nm + ".mp4") if j % 2 == 0 else None,
                             dur=min(c.win, c.dur - w["t0"]), prompt=w["prompt"], neg=c.neg + ", orchestra, theme music",
                             seed=777 + 31 * j + w["i"], steps=30, cfg=5.0))
    c.batch(jobs, clear_sub="cand")
    tags = c.tags("cand/*.flac")
    for nm in names:
        cands = sorted(((score(tags.get(f"{nm}_c{j}", {}), c.want.get(nm, []), MUSIC), j) for j in range(k)), reverse=True)
        best = cands[0][1]
        print(nm, "选 c%d 分 %.2f" % (best, cands[0][0]), list(tags.get(f"{nm}_c{best}", {}).items())[:4])
        c.sh("cp " + shlex.quote(f"{c.out_root()}/cand/{nm}_c{best}.flac")
             + " " + shlex.quote(f"{c.out_root()}/{nm}.flac"), 60)


def cmd_events(c, names=None, k=6):
    evs = [e for e in c.events if not names or e[0] in names]
    jobs = []
    for ei, (name, t0, dur, prompt, _g, _w) in enumerate(evs):
        for j in range(k):
            jobs.append(dict(name=f"ev_{name}_c{j}", sub="evc", video=c.in_path(f"ev_{name}.mp4") if j % 2 == 0 else None,
                             dur=min(dur, c.dur - t0), prompt=prompt, neg=c.neg + ", orchestra, theme music",
                             seed=4242 + 97 * j + 13 * ei, steps=30, cfg=5.0))
    c.batch(jobs, clear_sub="evc")
    tags = c.tags("evc/*.flac")
    c.out.mkdir(parents=True, exist_ok=True)
    for name, _t0, _d, _p, _g, want in evs:
        pen = [m for m in MUSIC if not (name.startswith("bell") and m in BELLISH)]
        cands = sorted(((score(tags.get(f"ev_{name}_c{j}", {}), want, pen), j) for j in range(k)), reverse=True)
        best = cands[0][1]
        print(name, "选 c%d 分 %.2f" % (best, cands[0][0]), list(tags.get(f"ev_{name}_c{best}", {}).items())[:4])
        c.get(f"evc/ev_{name}_c{best}.flac", c.out / f"ev_{name}.flac")


def cmd_fetch(c):
    c.out.mkdir(parents=True, exist_ok=True)
    for w in c.windows():
        c.get(f"{w['name']}.flac", c.out / f"{w['name']}.flac")
    print("已拉回", len(c.windows()), "个窗口 →", c.out)


def decode(path, sr):
    raw = subprocess.run([ffmpeg(), "-loglevel", "error", "-i", _noopt(path), "-f", "f32le", "-ac", "1", "-ar", str(sr), "-"],
                         check=True, capture_output=True).stdout
    return np.frombuffer(raw, np.float32).astype(np.float64)


def cmd_mix(c):
    sr, n = c.sr, int(c.dur * c.sr)
    acc = np.zeros(n, np.float64)
    xf = int((c.win - c.step) * sr)
    for w in c.windows():                      # 环境声：各窗先拉到同一响度、再按段落增益；正弦/余弦交叉淡化（总功率不塌）
        a = decode(c.out / f"{w['name']}.flac", sr)
        a = a / (np.sqrt(np.mean(a * a)) + 1e-9) * .08 * 10 ** (w["gain"] / 20)
        i0 = int(w["t0"] * sr)
        a = a[:max(0, min(len(a), n - i0))]
        env = np.ones(len(a))
        k = min(xf, len(a) // 2)
        ramp = np.sin(np.linspace(0, np.pi / 2, k))
        if w["t0"] > 0:
            env[:k] = ramp
        env[-k:] = ramp[::-1]
        acc[i0:i0 + len(a)] += a * env
    for name, t0, dur, _p, gain, _w in c.events:   # 场景音效：同样拉到统一响度后按增益叠上，头尾淡入淡出
        f = c.out / f"ev_{name}.flac"
        if not f.exists():
            print("  (缺)", name)
            continue
        a = decode(f, sr)
        a = a / (np.sqrt(np.mean(a * a)) + 1e-9) * .08 * 10 ** (gain / 20)
        i0 = int(t0 * sr)
        a = a[:max(0, min(len(a), n - i0))]
        fi, fo = int(.4 * sr), int((2.5 if name.startswith("bell") else 1.2) * sr)
        env = np.ones(len(a))
        env[:fi] = np.linspace(0, 1, fi)
        env[-fo:] = np.minimum(env[-fo:], np.linspace(1, 0, fo))
        acc[i0:i0 + len(a)] += a * env
    fade = int(1.0 * sr)
    acc[:fade] *= np.linspace(0, 1, fade)
    acc[-fade:] *= np.linspace(1, 0, fade)
    acc *= 10 ** (c.rms_db / 20) / (np.sqrt(np.mean(acc * acc)) + 1e-9)
    c.work.mkdir(parents=True, exist_ok=True)
    wav = c.work / "mix.wav"
    subprocess.run([ffmpeg(), "-y", "-loglevel", "error", "-f", "f32le", "-ar", str(sr), "-ac", "1", "-i", "-",
                    "-af", f"alimiter=limit={c.limit}:attack=5:release=80:level=0,pan=stereo|c0=c0|c1=c0",
                    "-ar", str(sr), "-c:a", "pcm_s16le", str(wav)], input=acc.astype(np.float32).tobytes(), check=True)
    subprocess.run([ffmpeg(), "-y", "-loglevel", "error", "-i", _noopt(c.video), "-i", _noopt(wav), "-map", "0:v", "-map", "1:a",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart",
                    str(c.out_video)], check=True)
    r = subprocess.run([ffmpeg(), "-hide_banner", "-i", _noopt(c.out_video), "-vn", "-af", "ebur128=peak=true:framelog=quiet",
                        "-f", "null", "-"], capture_output=True, text=True).stderr
    lines = [ln.strip() for ln in r.splitlines() if ln.strip().startswith(("I:", "Peak:"))]
    print("并入 →", c.out_video, " ".join(lines[:2]))


def cmd_plan(c):
    for w in c.windows():
        print(f"{w['name']} {w['t0']:6.1f}-{w['t0'] + c.win:6.1f}s  增益 {w['gain']:+.0f}dB  {w['prompt'][:90]}")
    for name, t0, dur, p, g, _w in c.events:
        print(f"音效 {name:12s} {t0:6.1f}s +{dur:.0f}s  {g:+.0f}dB  {p[:80]}")


def main():
    c = Cfg(sys.argv[1])
    cmd, args = sys.argv[2], sys.argv[3:]
    if cmd == "plan":
        cmd_plan(c)
    elif cmd == "cut":
        cmd_cut(c)
    elif cmd == "run":
        cmd_run(c, args or None)
    elif cmd == "tag":
        cmd_tag(c)
    elif cmd == "search":
        cmd_search(c, args)
    elif cmd == "events":
        cmd_events(c, args or None)
    elif cmd == "fetch":
        cmd_fetch(c)
    elif cmd == "mix":
        cmd_mix(c)
    elif cmd == "auto":
        t0 = time.time()
        cmd_cut(c)
        cmd_run(c)
        bad = cmd_tag(c)
        if bad:
            cmd_search(c, bad)
        if c.events:
            cmd_events(c)
        cmd_fetch(c)
        cmd_mix(c)
        print(f"全部完成 {time.time() - t0:.0f}s")
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
