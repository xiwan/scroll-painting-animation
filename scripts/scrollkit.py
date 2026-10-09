"""scrollkit —— 横向长卷"等距游戏风"动画的可复用零件。只依赖 numpy + Pillow。

从《清明上河图》180 秒长卷动画里抽出来的、与具体题材无关的部分：

  Iso / Camera        等距投影（网格 ↔ 屏幕）、镜头匀速横移时"某处何时到画面中央"
  DistrictField       功能团：沿卷轴排的若干团，边界随位置扭曲、相邻团交叠；可加"城墙"这类硬边界
  SpritePicker        按功能团亲和度挑图：附近用过的同一张降权、没出场过的加权，ensure_all_used 补位
  InkItem / draw_ink  题诗题字逐字"晕墨→收笔"显现，印章最后钤上
  mist_blob / top_mist  题字背后的雾衬（实心 + 柔边 + 噪声扰边）、顶部点缀式云气
  DayNight            昼 → 夜 → 黎明 → 晨的时间表、调色、发光层叠加
  lantern_mask / glow 从精灵里找灯笼像素（连通块筛形状）、柔光斑
  lowfreq, find_cjk_font, smoothstep

所有坐标约定：网格 (gx, gy) 是地面平面；屏幕世界坐标 (x, y) = 未乘像素比例的屏幕坐标；
内部像素 = 世界坐标 × R（R = 每世界单位像素数，含超采样）。u = gx − gy 与屏幕 x 成正比（卷轴方向），
v = gx + gy 与屏幕 y 成正比（纵深）。
"""
from __future__ import annotations

import glob
import json
import math
import os
import random
import subprocess
import warnings
from dataclasses import dataclass, field

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

C30 = math.cos(math.radians(30))


def smoothstep(x):
    x = min(max(float(x), 0.0), 1.0)
    return x * x * (3 - 2 * x)


# ------------------------------------------------------------------ 投影与镜头
class Iso:
    """2:1 风格的等距投影：网格 +gx → 屏幕右下，+gy → 屏幕左下。"""
    EX = np.array([C30, .5])        # 网格 +gx 一个单位在屏幕上的位移
    EY = np.array([-C30, .5])       # 网格 +gy
    UP = np.array([0.0, -1.0])      # 竖直向上一个单位

    @staticmethod
    def g2s(gx, gy):
        return np.array([C30 * (gx - gy), .5 * (gx + gy)])

    @staticmethod
    def s2g(p):
        a, b = p[0] / C30, 2 * p[1]
        return np.array([(a + b) / 2, (b - a) / 2])

    @staticmethod
    def uv(u, v):
        """(u = gx − gy, v = gx + gy) → 网格。"""
        return ((u + v) / 2.0, (v - u) / 2.0)


@dataclass
class Camera:
    """镜头沿屏幕 x 匀速平移。view_w 为画面宽（世界单位），speed 为世界单位/秒。"""
    speed: float
    view_w: float
    fps: int = 30

    def left(self, t):
        return self.speed * t

    def t_center(self, x):
        """世界 x 处的东西在第几秒走到画面正中。"""
        return (x - self.view_w / 2) / self.speed

    def t_enter(self, x, at=.8):
        """世界 x 处走到画面宽度 at 处（0 左缘、1 右缘）的时刻。"""
        return (x - at * self.view_w) / self.speed

    def x_at(self, t, at=.5):
        return self.speed * t + at * self.view_w


# ------------------------------------------------------------------ 功能团
@dataclass
class DistrictField:
    """沿卷轴（u）排的功能团。districts = [(名, u0, u1), ...]（u0 < u1，可重叠可留缝）。
    edge：相邻团过渡带宽度（u）；warp：边界扭曲幅度（u），让团的边缘不是一刀切的直线；
    clamps：硬边界列表 [(谓词 g→bool, u_min, u_max)]，满足谓词的点把扭曲后的 u 夹到区间内
    （例如城墙内的点 u 至少 12750，墙外至多 12250，城内外的团就不会互相渗透）。"""
    districts: list
    edge: float = 420.0
    warp: float = 260.0
    clamps: list = field(default_factory=list)

    def _uw(self, g):
        u, v = g[0] - g[1], g[0] + g[1]
        k = self.warp / 260.0
        uw = u + k * (260 * math.sin(v / 310 + 1.3) + 170 * math.sin((u + v) / 520 + .7) + 90 * math.sin(v / 97 + u / 800))
        for pred, lo, hi in self.clamps:
            if pred(g):
                uw = min(max(uw, lo), hi)
        return uw

    def weights(self, g):
        """网格点 g 属于各团的权重（和为 1）。"""
        uw = self._uw(g)
        out = {}
        for d in self.districts:
            name, a, b = d[0], d[1], d[2]          # 允许第 4 项是注释（见 assets/districts.*.json）
            w = smoothstep((uw - a) / self.edge + .5) * smoothstep((b - uw) / self.edge + .5)
            if w > 1e-3:
                out[name] = w
        s = sum(out.values()) or 1.0
        return {k: w / s for k, w in out.items()}

    def of(self, g):
        w = self.weights(g)
        return max(w.items(), key=lambda kv: kv[1])[0] if w else self.districts[-1][0]

    def mix(self, g, profiles, key, default=0.0):
        """按团权重混合某个数值属性，profiles = {团名: {key: 值}}。"""
        return sum(m * profiles.get(d, {}).get(key, default) for d, m in self.weights(g).items())

    def mix_dist(self, g, profiles, key):
        """混合"类别 → 概率"字典（例如房屋类型比例），返回未归一的字典。"""
        out = {}
        for d, m in self.weights(g).items():
            for k, p in profiles.get(d, {}).get(key, {}).items():
                out[k] = out.get(k, 0.0) + m * p
        return out


def load_district_config(src, clamps=None):
    """读 `assets/districts.*.json`（或已 load 好的 dict），拼出布局要用的几样东西。

    返回 dict：`field`（DistrictField）、`profiles`、`groups`、`kinds`、`aff`，以及原样带回的其余键。
    约定与注意：
      - `districts` 每项是 `[团名, u起, u止]`，第 4 项若存在当注释忽略；
      - `edge_u` → `DistrictField.edge`，`warp_u` → `warp`（没有就用默认）；
      - **硬边界（城墙那类）无法写进 JSON**（需要一个谓词函数），由调用方传
        `clamps=[(lambda g: g[0] > WALL_GX, 12750, 1e9), ...]`；JSON 里的 `wall_clamp_说明` 只是文字说明。
      - `landmarks_timeline_说明`、`lay_probs_mansion_court` 是给人读的参考，scrollkit 不消费。

    用法：
        cfg = load_district_config("assets/districts.qingming.json",
                                   clamps=[(lambda g: g[0] > 9000, 12750, 1e18),
                                           (lambda g: g[0] <= 9000, -1e18, 12250)])
        field = cfg["field"]
        picker = SpritePicker(cfg["groups"], cfg["kinds"], cfg["aff"], has=my_has)
    """
    if isinstance(src, (str, os.PathLike)):
        with open(src, encoding="utf-8") as f:
            src = json.load(f)
    d = dict(src)
    districts = [(x[0], float(x[1]), float(x[2])) for x in d.pop("districts")]
    kw = {}
    if "edge_u" in d:
        kw["edge"] = float(d.pop("edge_u"))
    if "warp_u" in d:
        kw["warp"] = float(d.pop("warp_u"))
    groups = d.pop("groups", {})
    kinds = {g: set(v) for g, v in d.pop("group_kinds", {}).items()}
    aff = d.pop("affinity", {})
    profiles = d.pop("profiles", {})
    missing = [g for g in groups if g not in kinds]
    if missing:
        raise ValueError(f"group_kinds 缺这些用途组：{missing}")
    return dict(field=DistrictField(districts, clamps=list(clamps or []), **kw),
                profiles=profiles, groups=groups, kinds=kinds, aff=aff, rest=d)


# ------------------------------------------------------------------ 选图
class SpritePicker:
    """groups = {用途组: [图名...]}；kinds = {用途组: {能充当的房屋类型...}}；aff = {团名: {用途组: 亲和度}}。
    pick(kind, g, field, rnd) 在同类型的图里按"所在团的亲和度"挑：同一张图在 rep_r 内每出现一次权重 ×rep_k，
    还没出场的 ×first_boost。big/small 规则通过 size_ok(name) 回调实现。"""

    def __init__(self, groups, kinds, aff, rep_r=360.0, rep_k=.2, first_boost=3.0, has=lambda n: True):
        self.groups, self.kinds, self.aff = groups, kinds, aff
        self.group_of = {n: g for g, ns in groups.items() for n in ns}
        self.pool = {}
        for g, ns in groups.items():
            for k in kinds[g]:
                self.pool.setdefault(k, []).extend(ns)
        self.rep_r, self.rep_k, self.first_boost, self.has = rep_r, rep_k, first_boost, has
        self.count = {}
        self._bins = {}
        self.records = []

    def _near(self, g, nm):
        c = self.rep_r
        cx, cy = int(g[0] // c), int(g[1] // c)
        n = 0
        for i in (cx - 1, cx, cx + 1):
            for j in (cy - 1, cy, cy + 1):
                for x, y, m in self._bins.get((i, j), ()):
                    if m == nm and (x - g[0]) ** 2 + (y - g[1]) ** 2 < c * c:
                        n += 1
        return n

    def affinity(self, nm, g, field_):
        grp = self.group_of[nm]
        return sum(m * self.aff.get(d, {}).get(grp, 0.0) for d, m in field_.weights(g).items())

    def pick(self, kind, g, field_, rnd, size_ok=lambda nm: True, record=None):
        names, wts = [], []
        for nm in self.pool.get(kind, ()):
            if not self.has(nm) or not size_ok(nm):
                continue
            w = self.affinity(nm, g, field_)
            if w <= 0:
                continue
            w *= self.rep_k ** min(self._near(g, nm), 3)
            if self.count.get(nm, 0) == 0:
                w *= self.first_boost
            names.append(nm)
            wts.append(w)
        if not names:
            return None
        nm = rnd.choices(names, wts)[0]
        self.note(nm, g)
        if record is not None:
            self.records.append(dict(record, name=nm, kind=kind, g=np.asarray(g, float)))
        return nm

    def note(self, nm, g):
        c = self.rep_r
        self._bins.setdefault((int(g[0] // c), int(g[1] // c)), []).append((g[0], g[1], nm))
        self.count[nm] = self.count.get(nm, 0) + 1

    def ensure_all_used(self, field_, swap, size_ok=lambda nm, rec: True, present=()):
        """每张图至少出场一次：还没用上的，找一栋同类型、所在团不排斥它（亲和度最高）、且那张图用了不止一次的
        记录，调用 swap(record, 新图名) 换掉。present：已经由别的途径（地标等）出场的图名。返回仍没用上的。"""
        left = []
        for nm in sorted(self.group_of):
            if not self.has(nm) or self.count.get(nm, 0) > 0 or nm in present:
                continue
            ks = self.kinds[self.group_of[nm]]
            best = None
            for rec in self.records:
                if rec["kind"] not in ks or self.count.get(rec["name"], 0) < 2 or not size_ok(nm, rec):
                    continue
                sc = self.affinity(nm, rec["g"], field_) + .001 * self.count[rec["name"]]
                if sc > 0 and (best is None or sc > best[0]):
                    best = (sc, rec)
            if best is None:
                left.append(nm)
                continue
            rec = best[1]
            swap(rec, nm)
            self.count[rec["name"]] -= 1
            self.count[nm] = 1
            rec["name"] = nm
        return left


# ------------------------------------------------------------------ 字体
def find_cjk_font(prefer=("Kaiti", "STKaiti", "Songti", "Libian", "Hiragino Sans GB", "STHeiti", "Noto Serif CJK",
                          "Noto Sans CJK", "SourceHanSerif", "SourceHanSans", "WenQuanYi", "simkai", "simsun",
                          "msyh", "msjh", "Arial Unicode")):
    """找一个能写 CJK 的字体文件，返回 (路径, ttc 索引) 或 None。

    查找顺序：环境变量 SCROLLKIT_FONT（路径，可带 `:索引`）→ 各平台字体目录 → fc-match。
    macOS 的楷体/隶书在 AssetsV2 里；Linux 建议装 Noto CJK（Debian/Ubuntu: fonts-noto-cjk，
    Fedora: google-noto-sans-cjk-fonts）；Windows 用自带的 simkai/msyh。
    """
    env = os.environ.get("SCROLLKIT_FONT")
    if env:
        path, _, idx = env.rpartition(":")
        if path and idx.isdigit():
            return path, int(idx)
        return env, 0
    roots = ["/System/Library/AssetsV2/com_apple_MobileAsset_Font*/*/AssetData",   # macOS
             "/System/Library/Fonts", "/Library/Fonts", os.path.expanduser("~/Library/Fonts"),
             "/usr/share/fonts/**", "/usr/local/share/fonts/**",                   # Linux
             os.path.expanduser("~/.local/share/fonts/**"), os.path.expanduser("~/.fonts/**"),
             os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"),        # Windows
             os.path.expanduser(r"~\AppData\Local\Microsoft\Windows\Fonts")]
    files = []
    for r in roots:
        for ext in ("*.tt[cf]", "*.otf", "*.otc"):
            files += glob.glob(os.path.join(r, ext), recursive=True)
    for key in prefer:
        for f in files:
            if key.lower().replace(" ", "") in os.path.basename(f).lower().replace(" ", ""):
                return f, 0
    try:                                     # 最后问一下 fontconfig（Linux 上最可靠）
        out = subprocess.run(["fc-match", "-f", "%{file}", ":lang=zh"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        if out and os.path.isfile(out):
            return out, 0
    except (OSError, subprocess.SubprocessError):
        pass
    return None


class FontBank:
    """按 (字体, 像素大小) 缓存 ImageFont。"""

    def __init__(self, path_idx=None, required=True):
        self.path_idx = path_idx or find_cjk_font()
        if not self.path_idx:
            msg = ("找不到任何 CJK 字体，题诗/题签会画成空白或豆腐块。请装一个中文字体"
                   "（Debian/Ubuntu: apt install fonts-noto-cjk；Fedora: google-noto-sans-cjk-fonts；"
                   "macOS 自带楷体；Windows 自带 simkai），或用 SCROLLKIT_FONT=/path/to/font.ttf 指定。"
                   " 只画无字画面可以传 FontBank(required=False) 跳过。")
            if required:
                raise RuntimeError(msg)
            warnings.warn(msg, stacklevel=2)
        self._c = {}

    def get(self, px):
        px = max(6, int(round(px)))
        if px not in self._c:
            if self.path_idx:
                self._c[px] = ImageFont.truetype(self.path_idx[0], px, index=self.path_idx[1])
            else:
                self._c[px] = ImageFont.load_default()
        return self._c[px]


# ------------------------------------------------------------------ 印章
def draw_seal(d: ImageDraw.ImageDraw, x, y, size, text, fonts: FontBank, R=1.0):
    """朱文方印：红底、白字、内框，边上几处细缺口。x, y, size 为世界单位，d 画在内部像素上。
    这是中式题款专有的零件——非中国题材应整体换掉（纹章、蜡封、签名），不要只换字。
    文字分支只处理 1/2/4 个字（四字右起两行）。"""
    rr = random.Random(sum(map(ord, text)))
    X, Y, S = x * R, y * R, size * R
    red = (172, 42, 32, 232)
    d.rectangle([X, Y, X + S, Y + S], fill=red)
    d.rectangle([X + S * .07, Y + S * .07, X + S * .93, Y + S * .93], outline=(246, 234, 216, 255), width=max(1, int(S * .035)))
    for _ in range(3):
        px, py = X + rr.uniform(.1, .9) * S, Y + rr.choice([.07, .93]) * S
        d.rectangle([px - S * .03, py - S * .03, px + S * .03, py + S * .03], fill=red)
    col = (248, 238, 222, 255)
    if len(text) == 4:                       # 四字印：右起两行
        for j, pair in enumerate((text[:2], text[2:])):
            for i, ch in enumerate(pair):
                d.text((X + S * (.71 - j * .42), Y + S * (.12 + i * .41)), ch, font=fonts.get(S * .36), fill=col, anchor="mt")
    elif len(text) == 2:
        for i, ch in enumerate(text):
            d.text((X + S / 2, Y + S * (.1 + i * .42)), ch, font=fonts.get(S * .40), fill=col, anchor="mt")
    else:
        d.text((X + S / 2, Y + S * .16), text[:1], font=fonts.get(S * .62), fill=col, anchor="mt")


# ------------------------------------------------------------------ 题字逐字显现
class InkItem:
    """一块题字（诗、小题、题跋）：画在自己的小图层上，记下每个字、每方印的位置和先后，供逐字显现。
    坐标为世界单位；R 为内部像素/世界单位。t0 开始写的时刻，speed 每秒写几个字。

    `text()` 按字形实测包围盒记录逐字蒙版，所以等宽方块汉字和宽度不等的拉丁字母都对得上；
    但 `column()` 是竖排（右起、自上而下）的中式版式，横排题材请改用 `row()`。"""

    def __init__(self, x0, y0, x1, y1, t0, speed, fonts: FontBank, R=1.0):
        self.x0, self.y0, self.R, self.fonts = float(x0), float(y0), R, fonts
        self.im = Image.new("RGBA", (int((x1 - x0) * R) + 2, int((y1 - y0) * R) + 2), (0, 0, 0, 0))
        self.d = ImageDraw.Draw(self.im)
        self.glyphs, self.seals = [], []
        self.t0, self.speed = t0, speed

    def text(self, x, y, ch, size, fill=(52, 44, 38, 245)):
        """在世界坐标 (x, y)（字的顶边中点）写一个字。按调用顺序就是书写顺序（竖排：右起、自上而下）。"""
        R = self.R
        X, Y = (x - self.x0) * R, (y - self.y0) * R
        f = self.fonts.get(size * R)
        self.d.text((X, Y), ch, font=f, fill=fill, anchor="mt")
        try:                                  # 实测字形框：拉丁字母宽度不等，不能按方块字估
            bx0, by0, bx1, by1 = self.d.textbbox((X, Y), ch, font=f, anchor="mt")
            pad = size * R * .08
            self.glyphs.append((int(bx0 - pad), int(by0 - pad), int(bx1 + pad), int(by1 + pad)))
        except (AttributeError, ValueError):  # 老版本 Pillow / 位图字体：退回方块字估算
            self.glyphs.append((int(X - size * .56 * R), int(Y - size * .1 * R),
                                int(X + size * .56 * R), int(Y + size * 1.12 * R)))

    def column(self, x, y, text, size, step=None, fill=(52, 44, 38, 245)):
        """竖排一列（自上而下）。多列请从右往左依次调用，这就是中式题诗的书写顺序。"""
        step = step or size * 1.17
        for i, ch in enumerate(text):
            self.text(x, y + i * step, ch, size, fill)

    def row(self, x, y, text, size, step=None, fill=(52, 44, 38, 245)):
        """横排一行（自左向右），按实际字宽推进——非中文题材用这个代替 column。"""
        f = self.fonts.get(size * self.R)
        cx = x
        for ch in text:
            self.text(cx, y, ch, size, fill)
            adv = step * self.R if step else (f.getlength(ch) if hasattr(f, "getlength") else size * self.R)
            cx += adv / self.R

    def seal(self, x, y, size, text):
        R = self.R
        draw_seal(self.d, x - self.x0, y - self.y0, size, text, self.fonts, R)
        X, Y = (x - self.x0) * R, (y - self.y0) * R
        self.seals.append((int(X - 1), int(Y - 1), int(X + size * R + 1), int(Y + size * R + 1)))

    def finish(self):
        a = np.asarray(self.im).copy()
        sm = np.zeros(a.shape[:2], bool)
        for (x0, y0, x1, y1) in self.seals:
            sm[max(0, y0):y1, max(0, x0):x1] = True
        txt = a.copy()
        txt[sm, 3] = 0
        sharp = Image.fromarray(txt)
        bl = sharp.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.GaussianBlur(2.2 * self.R))     # 洇开的淡墨
        b = np.asarray(bl).astype(np.float32)
        b[..., :3] = b[..., :3] * .55 + np.float32([96, 84, 72]) * .45
        b[..., 3] *= .7
        seal = a.copy()
        seal[~sm, 3] = 0
        n = len(self.glyphs)
        return dict(x0=self.x0, y0=self.y0, R=self.R, w=self.im.width, h=self.im.height, full=self.im, sharp=np.asarray(sharp),
                    bleed=np.clip(b, 0, 255).astype(np.uint8), seal=Image.fromarray(seal), glyphs=self.glyphs,
                    seals=self.seals, t0=self.t0, dur=(n + 3.0) / self.speed, n=n)


def ink_image(it, t, overlap=2.5):
    """条目在时刻 t 的样子（RGBA）；还没开写返回 None。每个字先是洇开的淡墨，再收成清楚的笔画，
    前后 overlap 个字交叠着写；字都写完后印章由大略缩到原大、由淡到实"按"上去。"""
    p = (t - it["t0"]) / it["dur"]
    if p <= 0:
        return None
    if p >= 1:
        return it["full"]
    n, K = it["n"], overlap
    u = p * (n + K + 1.5)
    sa = np.zeros((it["h"], it["w"]), np.float32)
    ba = np.zeros_like(sa)
    for i, (x0, y0, x1, y1) in enumerate(it["glyphs"]):
        q = min(max((u - i) / K, 0.0), 1.0)
        if q <= 0:
            break
        ys, xs = slice(max(0, y0), y1), slice(max(0, x0), x1)
        sa[ys, xs] = np.maximum(sa[ys, xs], q ** 1.6)
        ba[ys, xs] = np.maximum(ba[ys, xs], math.sin(math.pi * q) * .9)
    sh, bl = it["sharp"].astype(np.float32), it["bleed"].astype(np.float32)
    aS, aB = sh[..., 3] / 255 * sa, bl[..., 3] / 255 * ba
    aO = aS + aB * (1 - aS)
    rgb = (sh[..., :3] * aS[..., None] + bl[..., :3] * (aB * (1 - aS))[..., None]) / np.maximum(aO, 1e-4)[..., None]
    im = Image.fromarray(np.dstack([np.clip(rgb, 0, 255), aO * 255]).astype(np.uint8))
    qs = min(max((u - n - .5) / 1.5, 0.0), 1.0)
    if qs > 0:
        s = 1.18 - .18 * smoothstep(qs)
        for (x0, y0, x1, y1) in it["seals"]:
            c = it["seal"].crop((x0, y0, x1, y1))
            w2, h2 = max(1, int(c.width * s)), max(1, int(c.height * s))
            c = c.resize((w2, h2), Image.BICUBIC)
            c.putalpha(c.getchannel("A").point(lambda v, q=qs: int(v * min(1.0, q * 1.6))))
            im.alpha_composite(c, (int((x0 + x1 - w2) / 2), int((y0 + y1 - h2) / 2)))
    return im


def draw_ink(frame: Image.Image, items, t, view_x_px):
    """把进入画面的题字贴到帧上（frame 为内部像素 RGB/RGBA；view_x_px 为画面左缘在长卷上的像素位置）。"""
    for it in items:
        px, py = int(round(it["x0"] * it["R"])) - view_x_px, int(round(it["y0"] * it["R"]))
        if px > frame.width or px + it["w"] < 0:
            continue
        im = ink_image(it, t)
        if im is not None:
            frame.paste(im, (px, py), im)


# ------------------------------------------------------------------ 雾衬与云气
def lowfreq(h, w, cell, seed):
    """低频噪声 uint8（h×w），cell 为格子像素数。"""
    r = np.random.RandomState(seed)
    small = (r.rand(int(h // cell) + 2, int(w // cell) + 2) * 255).astype(np.uint8)
    return np.asarray(Image.fromarray(small).resize((w, h), Image.BICUBIC))


def mist_blob(xx, yy, cx, cy, rx, ry, noise, solid=True):
    """题字背后的雾衬 alpha（0..1）。solid：中间实、边缘 smoothstep 淡出、边界用低频噪声扰乱（不像圆盘）；
    否则是从中心往外渐淡的软椭圆。xx, yy 为网格化的坐标数组（世界单位），noise 同形状 0..1。"""
    rr = ((xx - cx) / rx) ** 2 + ((yy - cy) / ry) ** 2
    if solid:
        f = np.clip((1 - rr - .45 * (noise - .5)) / .55, 0, 1)
        return f * f * (3 - 2 * f) * (.8 + .3 * noise)
    return np.clip(1 - rr, 0, 1) ** .6 * (.72 + .3 * noise)


def top_mist(yy, top, pres_1d, depth_1d, noise):
    """顶部只作点缀的云气：pres_1d（沿卷轴 0..1，一段有一段无）、depth_1d（厚度，世界单位）。
    卷边只留极淡一线。yy 为列向量（世界单位），其余为行向量或同形状。"""
    a = np.clip(1 - (yy - top) / depth_1d, 0, 1) ** 1.4 * pres_1d * (.25 + .6 * noise)
    return np.maximum(a, np.clip(1 - (yy - top) / 9, 0, 1) * .22)


def presence_1d(w, cell, seed, thr=.56, soft=.16):
    """一维"有/无"开关（0..1），大约 30~40% 的长度为有。"""
    n = lowfreq(4, w, cell, seed)[1].astype(np.float32) / 255.0
    p = np.clip((n - thr) / soft, 0, 1)
    return p * p * (3 - 2 * p)


# ------------------------------------------------------------------ 昼夜晨昏
@dataclass
class DayNight:
    """dusk=(开始, 全黑)，dawn=(开始, 天亮)（秒）。night(t) 0..1，dawn_glow(t) 黎明暖色强度 0..1。

    默认值是《清明上河图》180 秒片的时刻（约 65%/74% 处入夜、87%/96% 处转黎明）。
    换片长别照抄——用 `DayNight.for_duration(总秒数)` 按比例生成，或直接传绝对秒数。
    """
    dusk: tuple = (117.0, 134.0)
    dawn: tuple = (157.0, 173.0)
    tint: tuple = (.27, .31, .50)          # 夜色：乘到去饱和后的画面上
    warm: tuple = (1.10, .95, .84)         # 黎明暖色
    mist_tint: tuple = (.42, .46, .60)     # 夜里云气的颜色系数

    @classmethod
    def for_duration(cls, dur, dusk=(.65, .745), dawn=(.872, .961), **kw):
        """按片长比例放置昼夜：默认比例取自《清》180 秒片。dur<=0 时退回默认绝对秒数。"""
        if dur and dur > 0:
            return cls(dusk=(dur * dusk[0], dur * dusk[1]), dawn=(dur * dawn[0], dur * dawn[1]), **kw)
        return cls(**kw)

    def night(self, t):
        a, b = self.dusk
        x = smoothstep((t - a) / (b - a))
        if self.dawn:
            y = smoothstep((t - self.dawn[0]) / (self.dawn[1] - self.dawn[0]))
            x *= 1 - y
        return x

    def dawn_glow(self, t):
        if not self.dawn:
            return 0.0
        y = (t - self.dawn[0]) / (self.dawn[1] - self.dawn[0])
        return math.sin(math.pi * y) ** 1.3 if 0 < y < 1 else 0.0

    def grade(self, arr, t, emissive=None, bloom=None):
        """arr: float32 H×W×3 画面（只含画里的景物，不含卷上的题字云气）；emissive/bloom：同尺寸发光层（uint8/float）。
        夜色 = 去饱和 × 冷色；黎明再整体偏暖、暗部带紫；发光层只在夜里加（灯在天亮时随夜色熄灭）。"""
        n, w = self.night(t), self.dawn_glow(t)
        if n <= 0 and w <= 0:
            return arr
        lum = arr.mean(2, keepdims=True)
        out = arr * (1 - n) + (arr * .6 + lum * .4) * np.float32(self.tint) * n
        if w > 0:
            out *= 1 + (np.float32(self.warm) - 1) * w
            out += np.float32([14, 4, 10]) * w * n
        if n > 0 and emissive is not None:
            out += n * emissive.astype(np.float32) * .95
            if bloom is not None:
                out += n * bloom.astype(np.float32) * np.float32([1.9, 1.5, 1.0])
        return out

    def mist_k(self, t, protect=None):
        """夜里云气（雾衬、顶云）的颜色系数；protect（H×W 0..1）为题字衬雾处——那里不压暗（诗在纸色上）。"""
        n = self.night(t)
        k = 1 - (1 - np.float32(self.mist_tint)) * n
        if protect is None:
            return k
        return 1 - (1 - np.float32(self.mist_tint)) * n * (1 - protect[..., None])


# ------------------------------------------------------------------ 灯笼与光斑
#: 发光物体的颜色判据：(亮度下限, g/r 上限, b/r 上限, r-b 下限)。默认是中式红灯笼。
#: 换题材要换这组数：欧洲壁灯/火把偏暖黄 → LAMP_WARM；现代路灯/窗光近白 → LAMP_WHITE。
LANTERN_RED = (150, .60, .50, 90)
LAMP_WARM = (170, .95, .70, 40)
LAMP_WHITE = (205, 1.15, 1.15, -40)


def lantern_mask(im: Image.Image, R=1.0, max_units=13.0, color=LANTERN_RED):
    """精灵里的发光体像素（默认红橙灯笼：红橙、饱和、够亮）。逐个连通块筛：只留大致圆/椭圆、面积不大的小团——
    细长的（红柱、栏杆）和大片的（红墙、大殿）都剔掉，否则夜里会像霓虹灯。返回 L 模式蒙版。
    换题材时传 color=LAMP_WARM / LAMP_WHITE 或自定四元组，详见 references/lighting.md。"""
    lo, gk, bk, rb = color
    a = np.asarray(im.convert("RGBA"), np.float32)
    r, g, b, al = a[..., 0], a[..., 1], a[..., 2], a[..., 3]
    m = (al > 180) & (r > lo) & (g < r * gk) & (b < r * bk) & (r - b > rb)
    m = np.asarray(Image.fromarray((m * 255).astype(np.uint8)).filter(ImageFilter.MinFilter(3)).filter(ImageFilter.MaxFilter(3))) > 0
    keep = np.zeros_like(m)
    seen = np.zeros_like(m)
    H, W = m.shape
    amax = (max_units * R) ** 2
    for y0, x0 in zip(*np.nonzero(m)):
        if seen[y0, x0]:
            continue
        stack, comp = [(y0, x0)], []
        seen[y0, x0] = True
        while stack:
            y, x = stack.pop()
            comp.append((y, x))
            for yy, xx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                if 0 <= yy < H and 0 <= xx < W and m[yy, xx] and not seen[yy, xx]:
                    seen[yy, xx] = True
                    stack.append((yy, xx))
        ys = [p[0] for p in comp]
        xs = [p[1] for p in comp]
        h, w = max(ys) - min(ys) + 1, max(xs) - min(xs) + 1
        if 12 <= len(comp) <= amax and h <= 2.0 * w and w <= 2.2 * h and len(comp) >= .45 * h * w:
            keep[ys, xs] = True
    keep = np.asarray(Image.fromarray((keep * 255).astype(np.uint8)).filter(ImageFilter.MaxFilter(3))) > 0
    return Image.fromarray(((keep & (al > 120)) * 255).astype(np.uint8))


def glow(rx, ry, col, peak):
    """柔光斑：(RGB 常色图, 高斯 alpha 图)，rx/ry 为像素半径。用 paste(rgb, pos, alpha) 贴到发光层。"""
    w, h = int(rx * 2.6) + 2, int(ry * 2.6) + 2
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    g = np.exp(-(((xx - w / 2) / rx) ** 2 + ((yy - h / 2) / ry) ** 2) * 1.6) * peak
    return Image.new("RGB", (w, h), col), Image.fromarray((np.clip(g, 0, 1) * 255).astype(np.uint8))


# ------------------------------------------------------------------ 杂项
def shear(im: Image.Image, k):
    """竖直剪切（竖线保持竖直，水平方向斜率 +k）。注意：船/车过弯不要用连续剪切去"转"精灵——观感怪；
    直接在几张朝向图之间硬切即可（见 references/motion.md）。"""
    w, h = im.size
    H2 = int(h + abs(k) * w + 2)
    return im.transform((w, H2), Image.AFFINE, (1, 0, 0, -k, 1, k * w / 2 - (H2 - h) / 2), Image.BICUBIC)


def bottom_profile(im: Image.Image, thr=128):
    """每列最低不透明像素的行号（无则 −1）。对齐两张不同朝向的图时用（让船底/墙根接上，换图时不跳）。"""
    a = np.asarray(im.getchannel("A")) > thr
    has = a.any(0)
    return np.where(has, a.shape[0] - 1 - np.argmax(a[::-1], 0), -1)
