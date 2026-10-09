"""scroll_demo —— 长卷等距动画的最小可运行骨架（不需要任何素材：房屋/树/行人用程序画的占位图）。

它把正式项目的每一层都走了一遍，换题材时照着替换各层即可：
  1. 场景构建：沿卷轴的阶梯形主路 → 两侧排地块 → 按功能团抽房屋类型、挑图、决定空地（菜园/树）→ 地标 → 树
  2. 底图烘焙：纸色 + 功能团地色 + 路面（一次性画成整幅长卷）
  3. 动态：行人沿主路折线走（每帧按位置插进静态精灵的前后顺序）
  4. 卷上的东西：题字逐字晕墨显现（InkItem）、雾衬、顶部点缀云气；夜里题字衬雾不压暗
  5. 昼夜晨昏：发光层（灯笼像素 + 门前光斑）只算一次，每帧裁切叠加
  6. 输出：preview 指定秒数出 JPEG，render 走 ffmpeg 出 mp4

用法:
  python scroll_demo.py preview 2 6 9.5 12.5 --out /tmp/demo     # 出几帧预览
  python scroll_demo.py render --out /tmp/demo.mp4               # 出完整小样（默认 14 秒 1280×720）
  python scroll_demo.py stats                                     # 打印每秒房屋数、功能团、每张图出场次数
"""
import argparse
import math
import os
import random
import sys
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scrollkit as K  # noqa: E402

Iso = K.Iso
EX, EY, UP = Iso.EX, Iso.EY, Iso.UP
W, H, FPS, DUR = 1280, 720, 30, 14.0
R = 1.0                                   # 内部像素/世界单位（正式项目里常用 2.8：1080p × 2 倍超采样）
VW, VH = W / R, H / R
CAM = K.Camera(speed=160.0, view_w=VW, fps=FPS)
PW = VW + CAM.speed * DUR + 40            # 长卷宽（世界单位）
PWI = int(PW * R)

# ---------------- 功能团（u = gx − gy；屏幕 x = cos30·u）
UMAX = PW / K.C30
FIELD = K.DistrictField([("fields", -500, UMAX * .25), ("market", UMAX * .25, UMAX * .55),
                         ("city", UMAX * .55, UMAX * .74), ("temple", UMAX * .74, UMAX + 500)], edge=220, warp=120)
PROF = {   # dens 起房概率，kinds 房屋类型比例，tint 地色，light 夜里灯火
    "fields": dict(dens=.35, kinds=dict(farm=1.0), tint=(196, 204, 150), light=.2),
    "market": dict(dens=.9, kinds=dict(shop=.6, res=.4), tint=(222, 206, 170), light=1.0),
    "city": dict(dens=.95, kinds=dict(shop=.4, res=.6), tint=(214, 204, 182), light=.8),
    "temple": dict(dens=.6, kinds=dict(res=.7, shop=.3), tint=(210, 206, 186), light=.5),
}
# 占位"图"：名字 → (墙色, 屋顶色, 墙高, 屋顶高)；正式项目里这些是 ComfyUI 生成的精灵文件名
LOOKS = {"farm_a": ((214, 190, 150), (170, 150, 96), 26, 16), "farm_b": ((200, 176, 140), (150, 132, 84), 24, 18),
         "shop_a": ((150, 98, 60), (70, 74, 82), 34, 18), "shop_b": ((168, 70, 52), (66, 70, 80), 30, 20),
         "shop_c": ((120, 86, 58), (80, 84, 92), 40, 16), "res_a": ((236, 230, 214), (70, 74, 84), 30, 18),
         "res_b": ((226, 218, 196), (60, 64, 74), 44, 20), "res_c": ((210, 200, 180), (86, 90, 98), 26, 22)}
GROUPS = {"farm": ["farm_a", "farm_b"], "shop": ["shop_a", "shop_b", "shop_c"], "res": ["res_a", "res_b", "res_c"]}
KINDS = {"farm": {"farm"}, "shop": {"shop"}, "res": {"res"}}
AFF = {"fields": dict(farm=1.0, res=.15), "market": dict(shop=1.0, res=.5), "city": dict(shop=.6, res=1.0),
       "temple": dict(res=1.0, shop=.4)}

POEMS = [(.22, ["芳原綠野恣行時", "春入遙山碧四圍"], "宋程顥", "春"), (.7, ["梁園歌舞足風流", "夜深燈火上樊樓"], "宋劉子翬", "市")]
CAPTION = (.45, "街市", "市")
DN = K.DayNight(dusk=(DUR * .5, DUR * .64), dawn=(DUR * .82, DUR * .94))


# ---------------- 占位精灵
def box_sprite(la, lb, look, lantern=True):
    """在网格地块 la×lb 上画一栋占位房屋（两面墙 + 双坡顶 + 窗 + 灯笼），返回 (RGBA, 左上角相对地块角 P 的偏移)。"""
    wall, roof, h, rh = look
    P = np.zeros(2)
    A, B = EX * la, EY * lb
    corners = [P, P + A, P + B, P + A + B]
    top = [c + UP * h for c in corners]
    r0, r1 = P + B / 2 + UP * (h + rh), P + A + B / 2 + UP * (h + rh)
    pts = corners + top + [r0, r1]
    x0, y0 = min(p[0] for p in pts) - 6, min(p[1] for p in pts) - 6
    x1, y1 = max(p[0] for p in pts) + 6, max(p[1] for p in pts) + 6
    im = Image.new("RGBA", (int((x1 - x0) * R) + 1, int((y1 - y0) * R) + 1), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    T = lambda p: (float((p[0] - x0) * R), float((p[1] - y0) * R))
    sh = lambda c, k: tuple(int(min(255, v * k)) for v in c) + (255,)
    ol = (60, 48, 40, 255)
    d.polygon([T(p) for p in (P + B, P + A + B, top[3], top[2])], fill=sh(wall, 1.0), outline=ol)       # 正面（+gy）
    d.polygon([T(p) for p in (P + A, P + A + B, top[3], top[1])], fill=sh(wall, .82), outline=ol)       # 侧面（+gx）
    for k in range(max(1, int(la // 26))):                                                          # 窗
        c = P + B + A * ((k + .5) / max(1, int(la // 26))) + UP * h * .45
        d.rectangle([*T(c - EX * 4 + UP * 4), *T(c + EX * 4 - UP * 4)], fill=(70, 52, 40, 255))
    d.polygon([T(p) for p in (top[0], top[1], r1, r0)], fill=sh(roof, .9), outline=ol)               # 后坡
    d.polygon([T(p) for p in (top[2], top[3], r1, r0)], fill=sh(roof, 1.0), outline=ol)              # 前坡
    d.polygon([T(p) for p in (top[1], top[3], r1)], fill=sh(wall, .75), outline=ol)                  # 山墙
    if lantern:                                                                                      # 檐下灯笼
        for f in (.2, .8):
            c = T(P + B + A * f + UP * (h - 3))
            d.ellipse([c[0] - 3.2 * R, c[1] - 4 * R, c[0] + 3.2 * R, c[1] + 4 * R], fill=(214, 58, 40, 255))
    return im, (x0, y0)


def tree_sprite(size, rnd):
    im = Image.new("RGBA", (int(size * R), int(size * 1.4 * R)), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    w, h = im.size
    d.rectangle([w * .46, h * .55, w * .54, h], fill=(96, 72, 52, 255))
    for _ in range(9):
        cx, cy, rr = rnd.uniform(.25, .75) * w, rnd.uniform(.15, .55) * h, rnd.uniform(.16, .26) * w
        g = rnd.randint(-12, 12)
        d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=(84 + g, 124 + g, 82 + g, 235))
    return im


def walker_sprite(rnd):
    im = Image.new("RGBA", (int(10 * R), int(22 * R)), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    col = rnd.choice([(70, 90, 120), (150, 80, 60), (90, 110, 80), (200, 190, 160), (120, 70, 100)])
    d.rectangle([2 * R, 7 * R, 8 * R, 18 * R], fill=col + (255,))
    d.ellipse([2.5 * R, 1 * R, 7.5 * R, 6.5 * R], fill=(230, 200, 170, 255))
    d.line([3.5 * R, 18 * R, 3.5 * R, 22 * R], fill=(60, 50, 40, 255), width=max(1, int(R)))
    d.line([6.5 * R, 18 * R, 6.5 * R, 22 * R], fill=(60, 50, 40, 255), width=max(1, int(R)))
    return im


# ---------------- 场景
class Scene:
    def __init__(self, seed=2026):
        self.rnd = random.Random(seed)
        self.spr = []                       # (key, x_px, y_px, im)
        self.lots = []                      # 每个放房子的地块（统计用）
        self.gardens = []                   # 菜园：屏幕多边形
        self.fronts = []                    # 门前灯光 (屏幕点, 宽, 灯火系数)
        self.labels = []
        self.picker = K.SpritePicker(GROUPS, KINDS, AFF)
        self.road = self.main_road()

    def main_road(self):
        """阶梯形主路：+gx 与 −gy 交替（屏幕上一段往右下、一段往右上），整体沿卷轴前进。"""
        L, pts = 330.0, []
        g = np.array(Iso.uv(-200, VH * 1.05))
        k = 0
        while Iso.g2s(*g)[0] < PW + 300:
            pts.append(g.copy())
            g = g + (np.array([L, 0]) if k % 2 == 0 else np.array([0, -L]))
            k += 1
        return pts

    def add_png(self, im, px, py):
        """im 左上角放在内部像素 (px, py)，前后顺序按底边。"""
        self.spr.append((py + im.height, int(px), int(py), im))

    def place_house(self, gx, gy, la, lb):
        c = np.array([gx + la / 2, gy + lb / 2])
        kinds = FIELD.mix_dist(c, PROF, "kinds")
        kind = self.rnd.choices(list(kinds), list(kinds.values()))[0]
        P = Iso.g2s(gx, gy)
        rec = dict(P=P, la=la, lb=lb)
        nm = self.picker.pick(kind, c, FIELD, self.rnd, record=rec)
        if nm is None:
            return
        im, (ox, oy) = box_sprite(la, lb, LOOKS[nm])
        self.add_png(im, (P[0] + ox) * R, (P[1] + oy) * R)
        self.picker.records[-1]["idx"] = len(self.spr) - 1
        self.lots.append((c, nm))
        self.fronts.append((P + EY * lb + EX * la / 2 + EY * 10, la, FIELD.mix(c, PROF, "light")))

    def vacant(self, gx, gy, la, lb):
        c = np.array([gx + la / 2, gy + lb / 2])
        if FIELD.of(c) in ("fields", "temple") and self.rnd.random() < .6:          # 菜园
            P = Iso.g2s(gx, gy)
            self.gardens.append([P, P + EX * la, P + EX * la + EY * lb, P + EY * lb])
        else:                                                                     # 空场 + 树
            for _ in range(self.rnd.randint(1, 2)):
                q = Iso.g2s(gx + la * self.rnd.uniform(.2, .8), gy + lb * self.rnd.uniform(.2, .8))
                t = tree_sprite(self.rnd.uniform(34, 52), self.rnd)
                self.add_png(t, q[0] * R - t.width / 2, q[1] * R - t.height)

    def rows_along(self, a, b, side):
        """沿一段主路（网格轴向）的一侧排地块：门朝路。side=+1 在 +gy/+gx 一侧，−1 在另一侧。"""
        horiz = abs(a[1] - b[1]) < 1e-6           # 沿 gx 走的段
        lo, hi = sorted((a[0], b[0])) if horiz else sorted((a[1], b[1]))
        s = lo + 20
        while s < hi - 60:
            la = self.rnd.uniform(56, 96)
            lb = self.rnd.uniform(46, 64)
            if horiz:
                gy = a[1] + (16 if side > 0 else -16 - lb)
                gx, wa, wb = s, la, lb
            else:
                gx = a[0] + (16 if side > 0 else -16 - lb)
                gy, wa, wb = s, lb, la
            c = np.array([gx + wa / 2, gy + wb / 2])
            if self.rnd.random() < FIELD.mix(c, PROF, "dens"):
                self.place_house(gx, gy, wa, wb)
            else:
                self.vacant(gx, gy, wa, wb)
            s += la + self.rnd.uniform(4, 16) * (2.5 if FIELD.of(c) == "fields" else 1.0)

    def build(self):
        for a, b in zip(self.road[:-1], self.road[1:]):
            for side in (-1, 1):
                self.rows_along(a, b, side)
        # 地标：卷尾功能团中段的一座大殿（指定的图，不走选图器）
        u = UMAX * .8
        g = np.array(Iso.uv(u, VH * 1.35))
        im, (ox, oy) = box_sprite(150, 100, ((176, 58, 44), (60, 62, 70), 56, 34))
        P = Iso.g2s(*g)
        self.add_png(im, (P[0] + ox) * R, (P[1] + oy) * R)
        self.labels.append(("大殿", P + EX * 75 + UP * 110))
        # 每张图都要出场
        def swap(rec, nm):
            key, px, py, _ = self.spr[rec["idx"]]
            im2, (ox2, oy2) = box_sprite(rec["la"], rec["lb"], LOOKS[nm])
            self.spr[rec["idx"]] = (key, int((rec["P"][0] + ox2) * R), int((rec["P"][1] + oy2) * R), im2)
        left = self.picker.ensure_all_used(FIELD, swap)
        if left:
            print("  没处放的图:", left)
        for _ in range(int(PW / 40)):                  # 散树
            x, y = self.rnd.uniform(0, PW), self.rnd.uniform(60, VH - 40)
            t = tree_sprite(self.rnd.uniform(30, 48), self.rnd)
            self.add_png(t, x * R - t.width / 2, y * R - t.height)
        self.spr.sort(key=lambda s: s[0])
        self.x0s = np.array([s[1] for s in self.spr])
        self.x1s = np.array([s[1] + s[3].width for s in self.spr])


# ---------------- 底图、卷上云气与题字、发光层
def bake_ground(sc):
    """整幅长卷的地面一次画好：按功能团混合地色（逐 64 像素一列，正式项目里用向量化的分块烘焙）+ 低频明暗 +
    菜园 + 主路。每帧只是从这张大图上裁一块。"""
    base = np.empty((int(VH * R), PWI, 3), np.float32)
    xs = (np.arange(PWI) + .5) / R
    xk = np.arange(0, PWI, 16)
    cols = np.array([sum(m * np.float32(PROF[d]["tint"]) for d, m in
                         FIELD.weights(Iso.s2g(np.array([xs[x], VH * .55]))).items()) for x in xk])
    for ch in range(3):                                  # 每 16 像素算一次，再沿 x 线性插值，过渡处没有台阶
        base[..., ch] = np.interp(np.arange(PWI), xk, cols[:, ch])[None, :]
    n = K.lowfreq(base.shape[0], PWI, 40, 3).astype(np.float32) / 255.0
    base *= (1 + .08 * (n - .5))[..., None]
    im = Image.fromarray(np.clip(base, 0, 255).astype(np.uint8))
    d = ImageDraw.Draw(im)
    for poly in sc.gardens:
        d.polygon([(float(p[0] * R), float(p[1] * R)) for p in poly], fill=(132, 160, 96))
    for a, b in zip(sc.road[:-1], sc.road[1:]):                 # 主路：一条宽 28 的土路
        horiz = abs(a[1] - b[1]) < 1e-6
        w = 14.0
        q = [a - [0, w], b - [0, w], b + [0, w], a + [0, w]] if horiz else [a - [w, 0], b - [w, 0], b + [w, 0], a + [w, 0]]
        d.polygon([tuple(float(v * R) for v in Iso.g2s(*p)) for p in q], fill=(206, 186, 146))
    return im.filter(ImageFilter.GaussianBlur(.6))


def build_overlay(fonts):
    """卷上的云气（alpha）、题字衬雾的"保护"图（夜里不压暗），以及题字条目。"""
    Hp = int(VH * R)
    yy = ((np.arange(Hp) + .5) / R)[:, None]
    xx = ((np.arange(PWI) + .5) / R)[None, :]
    n = K.lowfreq(Hp, PWI, 50, 41).astype(np.float32) / 255.0
    pres = K.presence_1d(PWI, 220, 43)[None, :]
    depth = (18 + 50 * K.presence_1d(PWI, 90, 47))[None, :]
    a = K.top_mist(yy, 6, pres, depth, n)
    prot = np.zeros_like(a)
    items = []
    for fx, lines, by, seal in POEMS:
        cx = PW * fx
        top = VH - 30 - max(len(ln) for ln in lines) * 19
        f = K.mist_blob(xx, yy, cx - 20, VH - 86, 90, 100, n, solid=True)
        a, prot = np.maximum(a, f), np.maximum(prot, np.clip(f * 1.4, 0, 1))
        it = K.InkItem(cx - 80, top - 6, cx + 14, VH - 16, t0=CAM.t_enter(cx - 30, .85), speed=8.0, fonts=fonts, R=R)
        for j, ln in enumerate(lines):
            it.column(cx - j * 23, top, ln, 16, step=19)
        bx = cx - len(lines) * 23 - 2
        it.column(bx, top + 6, by, 11, step=13, fill=(66, 56, 48, 245))
        it.seal(bx - 8, top + 10 + len(by) * 13, 14, seal)
        items.append(it.finish())
    fx, text, seal = CAPTION
    cx = PW * fx
    f = K.mist_blob(xx, yy, cx - 8, 92, 70, 92, n, solid=False)
    a, prot = np.maximum(a, f), np.maximum(prot, np.clip(f * 1.4, 0, 1))
    it = K.InkItem(cx - 30, 30, cx + 14, 50 + len(text) * 24 + 20, t0=CAM.t_enter(cx, .8), speed=4.0, fonts=fonts, R=R)
    it.column(cx, 36, text, 20, step=24, fill=(58, 50, 44, 238))
    it.seal(cx - 28, 40 + len(text) * 24, 15, seal)
    items.append(it.finish())
    ov = np.empty((Hp, PWI, 4), np.uint8)
    ov[..., :3] = (248, 244, 234)
    ov[..., 3] = (np.clip(a, 0, .92) * 255).astype(np.uint8)
    return Image.fromarray(ov), (prot * 255).astype(np.uint8), items


def build_emissive(sc):
    """发光层：门前光斑（按功能团的灯火系数）+ 每张静态精灵自己的灯笼像素；先贴后面的、再贴前面的，
    每张精灵先把自己覆盖的区域涂黑（挡住后面的光），所以被房子、树挡住的灯看不见。"""
    E = Image.new("RGB", (PWI, int(VH * R)), (0, 0, 0))
    for c, w, lf in sc.fronts:
        rgb, al = K.glow(max(10, w * .4) * R, 7 * R, (255, 176, 96), .5 * lf)
        E.paste(rgb, (int(c[0] * R - rgb.width / 2), int(c[1] * R - rgb.height / 2)), al)
    cache = {}
    for key, px, py, im in sc.spr:
        E.paste((0, 0, 0), (px, py, px + im.width, py + im.height), im)
        k = id(im)
        if k not in cache:
            cache[k] = K.lantern_mask(im, R)
        E.paste(im.convert("RGB"), (px, py), cache[k])
    return np.asarray(E), np.asarray(E.filter(ImageFilter.GaussianBlur(7 * R)))


class Walkers:
    def __init__(self, sc, n=60, seed=7):
        rnd = random.Random(seed)
        pts = [Iso.g2s(*g) for g in sc.road]
        seg = [np.linalg.norm(b - a) for a, b in zip(pts[:-1], pts[1:])]
        self.pts, self.cum = pts, np.concatenate([[0], np.cumsum(seg)])
        self.w = [dict(s0=rnd.uniform(0, self.cum[-1]), v=rnd.choice([-1, 1]) * rnd.uniform(14, 26),
                       lat=rnd.uniform(-9, 9), im=walker_sprite(rnd), ph=rnd.uniform(0, 6)) for _ in range(n)]

    def at(self, s):
        s = s % self.cum[-1]
        i = int(np.searchsorted(self.cum, s, side="right") - 1)
        i = min(i, len(self.pts) - 2)
        a, b = self.pts[i], self.pts[i + 1]
        return a + (b - a) * (s - self.cum[i]) / max(1e-6, self.cum[i + 1] - self.cum[i])

    def items(self, t):
        out = []
        for w in self.w:
            p = self.at(w["s0"] + w["v"] * t) + np.array([0, w["lat"] * .5])
            bob = abs(math.sin(t * 9 + w["ph"])) * 1.2
            im = w["im"]
            out.append((p[1] * R, int(p[0] * R - im.width / 2), int((p[1] - bob) * R - im.height), im))
        return out


# ---------------- 每帧
G = {}


def setup(need_font=True):
    t0 = time.time()
    sc = Scene()
    sc.build()
    fonts = K.FontBank(required=need_font)   # 没有 CJK 字体时：出画面会报错并给出装字体的办法，stats 仍可跑
    G.update(sc=sc, ground=bake_ground(sc), walkers=Walkers(sc), fonts=fonts)
    G["overlay"], G["prot"], G["ink"] = build_overlay(fonts)
    G["E"], G["EB"] = build_emissive(sc)
    # SKILL.md 第 3 步的门槛：setup 要打印各层数量，便于"改了布局后某样东西消失"时一眼看出
    print(f"setup {time.time() - t0:.1f}s：精灵 {len(sc.spr)}，地块 {len(sc.lots)}，"
          f"街段 {max(0, len(sc.road) - 1)}，行人 {len(G['walkers'].w)}，"
          f"题签 {len(sc.labels)}，题字 {len(G['ink'])}，字体 {G['fonts'].path_idx}", flush=True)


def render_frame(fi):
    t = fi / FPS
    sc = G["sc"]
    cx = int(round(CAM.left(t) * R))
    fr = G["ground"].crop((cx, 0, cx + W, H))
    vis = np.nonzero((sc.x1s > cx) & (sc.x0s < cx + W))[0]
    items = [sc.spr[i] for i in vis] + [it for it in G["walkers"].items(t) if cx - 20 < it[1] < cx + W + 20]
    items.sort(key=lambda s: s[0])
    for _, px, py, im in items:
        fr.paste(im, (px - cx, py), im)
    arr = DN.grade(np.asarray(fr, np.float32), t, G["E"][:, cx:cx + W], G["EB"][:, cx:cx + W])
    fr = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
    ov = G["overlay"].crop((cx, 0, cx + W, H))
    if DN.night(t) > 0:
        oa = np.asarray(ov).copy()
        k = DN.mist_k(t, G["prot"][:, cx:cx + W].astype(np.float32) / 255.0)
        oa[..., :3] = (oa[..., :3].astype(np.float32) * k).astype(np.uint8)
        ov = Image.fromarray(oa)
    fr.paste(ov, (0, 0), ov)
    K.draw_ink(fr, G["ink"], t, cx)
    d = ImageDraw.Draw(fr)
    for text, p in sc.labels:                         # 题签：纸色竖条，画在云气之上，不受夜色影响
        x, y = p[0] * R - cx, p[1] * R
        if -30 < x < W + 30:
            fz = G["fonts"].get(11 * R)
            h = len(text) * 13 * R + 8
            d.rectangle([x - 8 * R, y - h, x + 8 * R, y], fill=(236, 228, 204), outline=(80, 64, 50))
            for i, ch in enumerate(text):
                d.text((x, y - h + 4 + i * 13 * R), ch, font=fz, fill=(40, 34, 30), anchor="mt")
    fade = min(1.0, t / .6, (DUR - t) / .6)
    out = np.asarray(fr, np.float32)
    if fade < 1:
        out = 246 + (out - 246) * max(0.0, fade)
    return np.clip(out, 0, 255).astype(np.uint8)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["preview", "render", "stats"])
    ap.add_argument("secs", nargs="*", type=float)
    ap.add_argument("--out", help="preview: 帧输出目录（默认 demo_frames/）；render: mp4 路径（默认 demo.mp4）")
    a = ap.parse_args()
    a.out = a.out or ("demo.mp4" if a.cmd == "render" else "demo_frames")
    setup(need_font=a.cmd != "stats")
    sc = G["sc"]
    if a.cmd == "stats":
        per = {}
        for c, nm in sc.lots:
            t = int(CAM.t_center(Iso.g2s(*c)[0]))
            per.setdefault(t, []).append(FIELD.of(c))
        for t in sorted(per):
            if 0 <= t <= DUR:
                print(f"  {t:3d}s 房屋 {len(per[t]):2d}  " + " ".join(sorted(set(per[t]))))
        print("每张图出场:", dict(sorted(sc.picker.count.items())))
        return
    if a.cmd == "preview":
        os.makedirs(a.out, exist_ok=True)
        for s in a.secs or [2, DUR * .55, DUR * .75, DUR * .95]:
            p = os.path.join(a.out, f"t{s:05.1f}.jpg")
            Image.fromarray(render_frame(int(round(s * FPS)))).save(p, quality=88)
            print(" ", p)
        return
    import imageio_ffmpeg
    cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-crf", "17", "-pix_fmt", "yuv420p", a.out]
    import subprocess
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    t0 = time.time()
    for fi in range(int(DUR * FPS)):
        p.stdin.write(render_frame(fi).tobytes())
    p.stdin.close()
    p.wait()
    print(f"完成 {a.out}（{time.time() - t0:.0f}s）")


if __name__ == "__main__":
    main()
