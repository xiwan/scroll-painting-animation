# 素材生产：ComfyUI 精灵、朝向图、贴图

工具：`scripts/comfy_client.py`（提交工作流 / 等队列 / 拉回 / 合成透明精灵），`scripts/contact_sheet.py`（对照表），
`scripts/tex_tile.py`（无缝贴图）。实际用过的提示词全集：`assets/prompts.qingming.json`。

## 1. 统一风格：提示词的固定结构

每条提示词 = **主体描述** + **约束句** + **风格句**，约束句和风格句全套共用、一个字都别改：

```
主体："A two-storey Song dynasty teahouse with an open upper gallery ..."
约束：FRONT  "the main front facade faces toward the lower left of the image"
      NOTXT  "no text, no letters, no calligraphy"
      SPRITE "single isolated object centered on a plain flat light beige background, no ground clutter, full object visible"
风格：STYLE  "isometric top-down 3/4 view at 45 degrees, Chinese Song dynasty, hand-painted 2D game art in the style of ..."
```

- **朝向写死**（门脸朝左下）：引擎靠"正面朝 +gy"来摆房子，朝向不统一就得逐张登记（见 §5）。
- **纯色底**：抠图才干净。底色和画卷纸色接近，抠图残边不显眼。
- Z-Image 没有负向提示——正向里写"no house"反而招来房子。人物、道具用不含建筑词的 STYLE_OBJ。
- 每个提示词出 **两个种子（a/b）**：同一功能的两张不同房子，比一张图缩放复用自然得多。
- 招牌文字：NOTXT 也挡不住模型偶尔写字（客栈招牌）；成片尺寸下看不清可以接受，近景要换种子。

## 2. 精灵类别与尺寸

| 类别 | 画布 | 目标尺寸 | 备注 |
|---|---|---|---|
| 建筑 `bld_*` | 1024² | 最长边 720 | 引擎按地块跨度缩放；体量差太多的（大院落、长仓）只放进够大的地块 |
| 道具 `prop_*` | 1024² 或 1344×768 | 最长边 520 | 车、轿、摊子、照壁、坟茔 |
| 人物图集 `chars_*` | 1344×768 | 高 300 | "一行四个、间隔很宽、互不接触"，抠图后按连通块拆成 4 张 |
| 船 | 1344×768 | 最长边 720 | 斜向图 + 船头调转图 + 正侧面图（见 §4） |
| 贴图 `tex_*` | 1024² / 1344×768 | 原尺寸 | 不抠图；本机做无缝（§6） |

`comfy_client.py sprite` 默认在**本机**缩（`--max` / `--height`）；下行很慢时才挪到服务器上做（见 §3 末）。

这些尺寸隐含了东亚小尺度民居的比例（房屋 ≈ 人高的 2.4 倍）。教堂广场、现代 CBD、宫殿群的体量关系完全不同，
照抄会让画面比例失真——换题材时先定"一栋典型建筑是人高的几倍"，再反推目标尺寸。

人物身高按"参考身高"登记（不同图集画得大小不一，按每张实测的人高统一缩放）；骑马、挑担、撑伞的整张会更高，单独登记。

### 登记表放哪

`FACE_R`/`BOW_L`/`BIG`/`SMALL` 这些登记就写在你的引擎脚本里（`scroll_demo.py` 的 `Scene` 那一层），
以 Python 字面量/集合的形式，和选图逻辑放在一起——不需要额外的文件格式。
素材本身按 `assets/cut/<类别>_<名字>.png` 存放（`contact_sheet.py`、`layout.md` §4 的统计都按这个前缀找图）。

## 3. 抠图与打包

工作流：文生图 → 同一张图跑 BiRefNet 得蒙版 → 合成 RGBA → 蒙版收一圈去毛边 → 去掉远离主体的小碎块 →
裁到不透明区域 → 缩到目标尺寸 → 存 PNG。`comfy_client.py sprite` 在本机做这些（PNG 带透明、无损；
要省体积可自己补一行转 WebP）。

**下行带宽很低时**（跨洋访问 GPU 机，常见 ~10KB/s）：把合成/裁边/缩小/转 WebP 放到服务器上用脚本做
（经 `ssm_run.sh` 执行），本机只拉 20–150KB 的小图。

## 4. 朝向图（Qwen-Image-Edit）

只有一个斜向朝向不够：行人背影、牲畜正反、船头调转、船的正侧面都要另出。用图像编辑模型"转身"，提示词必须带
**保持句**，否则衣服花纹、货物、人数会变：

- 人："Keep exactly the same character identity, the same clothing, colors, patterns, hat ..."
- 牲畜**不能**用人的保持句（里面的 "hat" 会让模型给马牛戴帽子）——用专门的 KEEP_ANIMAL。
- 船头调转："the bow end and the stern end trade places ... nobody is upside down"（不写这句会出倒立的人）。
- 正侧面："the long hull lies perfectly horizontal ... bow pointing to the right"。

编辑结果逐张看：转错方向、多出/少了人、上下颠倒的都要重做（换种子）。

## 5. 登记表（引擎侧）

- `FACE_R`：门脸实际朝右下的图（生成时没听话的），引擎放置时镜像方向相反。**每批新图都要过一遍对照表逐张判断**：
  看台阶、门、敞开的铺面在哪一面。
- `BOW_L` / 侧面图船头方向：船头在左的登记，否则过弯换图时船会倒着走。
- 大小类：`BIG`（只进大地块）、`SMALL`（大地块降权）。
- 用途组与能充当的房屋类型（见 layout.md 的 GROUPS/GKINDS）。

## 6. 贴图（地面、墙面）

1. ComfyUI 出一张俯视/正视的原图（"a flat frontal orthographic view of ... evenly lit, no perspective"）。
2. **本机做无缝**：`tex_tile.py --mode bond|grid|soft|wrapx --check`。
   - 不要让扩散模型修接缝（inpaint）——会在中间留下十字形的缝。
   - 错缝砖/石板：按缝切行、每行首尾在竖缝截断、统一宽度、每行随机平移。
   - 路面条带用 `wrapx`：纵向是路的横断面（两道车辙、两边草沿），只能沿路方向平铺。
3. 采样时色调三成向原调色板靠拢，明暗对比可调（和房屋、底图同一套颜色）。

## 7. 质检

每批新图出对照表（`contact_sheet.py`，长边 ≤1900）逐张看：风格统一？朝向？抠图残渣？混进文字？体量合理？
不合格的换种子重出，不要在引擎里将就。
