---
name: scroll-painting-animation
description: >
  把一幅长卷名画（或任何"沿一条路看过去"的全景题材）做成横向卷轴、等距游戏画风的动画短片：读原作排时间轴与功能团、
  用 ComfyUI 统一风格地生产建筑/人物/船/贴图精灵、用 Python（numpy + Pillow）搭等距场景并逐帧渲染、
  做疏密与聚团布局、行人车马船的运动、昼夜晨昏与万家灯火、题诗逐字晕墨显现、用 MMAudio 按画面生成环境声与场景音效，
  最后用统计、抽帧、跳帧检查、响度测量验收。附可复用库 scrollkit、可运行骨架 scroll_demo 和一套工具脚本。
  来源项目：《清明上河图》180 秒 1080p 等距动画（《烽火与炊烟》画风）。
  触发词：清明上河图动画、名画动起来、长卷动画、卷轴动画、千里江山图、姑苏繁华图、等距卷轴、isometric scroll、
  panoramic pan animation、2.5D 全景动画、横向平移长卷、ComfyUI 精灵、批量生成建筑素材、功能团布局、
  万家灯火夜景、题诗渐显、MMAudio 音效、按画面配音效。
---

# 长卷等距动画（scroll-painting-animation）

## 这类片子是什么

镜头沿长卷**匀速横移**，画面是等距（2.5D）的游戏风场景：地面、房屋、树、桥是静态精灵，行人车马船在上面动，
卷上的题签、题诗、云气是"纸面上的东西"，不属于画里的世界。三条贯穿全程的判断：

1. **时间就是空间**。镜头每秒走固定距离，所以"某样东西几秒出现、在画面里停几秒"由它在长卷上的位置决定。
   一切规划（功能团、高潮、题诗、昼夜、音效）都落在同一条时间轴上。
2. **疏密与聚团来自原作的空间逻辑**，不是随机撒点。先弄清那个社会怎么用空间（城乡梯度、交通节点、同业相聚、
   官署寺观的形制），再排功能团。
3. **我们看得到画面、听不到声音**。画面靠抽帧、统计、跳帧检查验收；声音只能靠分类模型打标签核对——
   交付时要说清哪些验证过、哪些没法验证。

## 流程（每步有"过不去就别往下做"的门槛）

### 1. 读原作，建时间轴表 → `references/source-analysis.md`

场景清单（名场面/地标、人物母题、环境）+ 空间逻辑 + 时间映射。产出一张表：
每段几秒、什么功能团、每 10 秒几栋房、哪些地标母题、夜里多亮、配什么声。

**门槛**：用户点名的每一条都在表里有位置；每个功能团 ≥8 秒；高潮在 55%–75%；题诗不压名场面。

### 2. 立风格，生产素材 → `references/assets.md`

固定的提示词结构（主体 + 朝向/无字/孤立物体约束 + 统一风格句），每个用途两个种子；朝向图用图像编辑模型转；
贴图本机做无缝。工具：`comfy_client.py`、`contact_sheet.py`、`tex_tile.py`；提示词库 `assets/prompts.qingming.json`。

**门槛**：每批新图过对照表——风格统一、朝向登记、无抠图残渣、无文字、体量合理。不合格的重出，不在引擎里将就。

### 3. 搭引擎骨架 → `references/engine.md`

先跑通 `scripts/scroll_demo.py`（无素材的 14 秒小样，所有层都有），再在它的层次上替换/加东西：
坐标与尺度、场景构建顺序与占地系统、底图烘焙、画家算法排序、每帧合成顺序、多进程出片。

**门槛**：`preview` 能出任意秒数的帧；setup 打印各层数量（精灵、街段、闲立人、船……）。

### 4. 布局：功能团、疏密、选图 → `references/layout.md`

`DistrictField`（软边 + 扭曲边界 + 城墙硬边界）驱动：起房概率、行距、房屋类型比例、院落/大宅比例、选图亲和度、夜里灯火。
空地做菜园/空场/树。`SpritePicker` 防相邻重复、首次出场加权，`ensure_all_used` 补位。
北宋都城的完整配置：`assets/districts.qingming.json`。

**门槛**：每 10 秒房屋数曲线贴近目标；**素材目录里每张建筑图都出场**（统计逐张核对，未出场列表为空）。

### 5. 动起来 → `references/motion.md`

行人分速度等级与车道；船过弯**在三张朝向图之间硬切**（不做淡化/剪切/扫换），换图按船底对齐；
桥下分层；名场面拆成"距离 → 动作"脚本，用跟拍条验。

### 6. 卷上的字 → `references/text.md`

选与时代相符的诗、逐字核对、繁体楷书；竖排右起、落款钤印；雾衬以整组字为中心；
**逐字晕墨显现**、印章最后按上；顶部云气只作点缀。

### 7. 光 → `references/lighting.md`

昼 → 夜 → 黎明 → 晨（首尾呼应）；发光层按画家顺序一次算好（被挡的灯看不见），灯笼按连通块形状筛（不让红柱变霓虹），
灯火多少按功能团；**夜色只染画里的景物，题诗衬雾不压暗**。

### 8. 声音 → `references/audio.md`

`sfx_mmaudio.py` + JSON 配置：10 秒窗、8 秒步长的环境声 + 定点的场景音效；AST 打标签、对不上的多出候选自动挑；
正弦/余弦交叉淡化、分段增益、限幅、并入。权重非商用，交付时说明。样例：`assets/sfx_config.example.json`。

**门槛**：没有窗口被标成配乐/雷声/静音；成片约 −17 LUFS、真峰值 < −0.5 dBFS。

### 9. 验收与交付 → `references/qa.md`

统计 → 抽帧（`frame_grid.py`）→ 全片 → `popcheck.py`（0 离群）→ 响度 → 回归清单。交付说明逐条回答用户的点名需求和
中途补充的要求，写明验证过什么、没验证什么，提醒云上机器的状态与费用（停不停由用户决定）→ `references/infra.md`。

## 快速判断：出问题先查哪

| 症状 | 先看 |
|---|---|
| 船过弯怪、叠影、换图跳一下 | motion.md §2 |
| 房子像复制粘贴 / 某张图从没出现 | layout.md §3–4 |
| 太密太匀、没有"团"的感觉 | layout.md §1–2，source-analysis.md §2 |
| 路口、拐角、石板街拼接生硬 | engine.md §3 |
| 题诗看不清、没和雾衬对齐、夜里被盖住 | text.md §3、§5 |
| 夜里红柱子发光 / 灯火太均匀 | lighting.md §3、§2 |
| 改了布局后某些东西消失 | qa.md §3 回归清单，engine.md §2 随机数流 |
| 音效出成配乐 / 雷声 / 没声 | audio.md §4 |
| 远端命令失败、ComfyUI 连不上 | infra.md §2–3 |
| 看图工具拒收图片 | qa.md §1 看图限制 |

完整的"症状 → 原因 → 处理"表在 `references/qa.md` §2。

## 与用户协作的习惯（来自实际项目）

- 用户会边看边提新要求（steering）。每条都记下，做完逐条回执；和之前的决定冲突时按最新的来，并说明。
- 大的方向（例如"按功能团重排"）先给方案表请用户拍板，再动手；小的修改直接做、做完说明。
- 每轮交付讲清：做了什么、在片子的第几秒能看到、有什么已知不足。
- 用户只说"在么 / 好了么"时，先用一两句交代当前进度，再继续。

## 文件索引

| 文件 | 内容 | 何时读 |
|---|---|---|
| `references/source-analysis.md` | 场景清单、空间逻辑、时间轴映射、迁移到其他题材 | 立项时 |
| `references/assets.md` | 提示词结构、精灵类别尺寸、抠图打包、朝向编辑、登记表、贴图、质检 | 生产素材时 |
| `references/engine.md` | 坐标尺度、构建顺序、占地、底图烘焙、前后顺序、每帧合成、出片性能 | 搭引擎、查渲染问题时 |
| `references/layout.md` | 功能团、疏密节奏、选图、每张图都用上、地标、布局回归 | 排布局时 |
| `references/motion.md` | 行人车马、船过弯硬切与对齐、桥、名场面脚本、风筝炊烟 | 做动态时 |
| `references/text.md` | 选诗、版式、雾衬、逐字晕墨、夜里保护 | 做题字时 |
| `references/lighting.md` | 昼夜晨昏、发光层、灯笼检测、天气 | 做夜景时 |
| `references/audio.md` | MMAudio 安装、窗口与事件、AST 核对、混音、许可 | 配音效时 |
| `references/qa.md` | 工具、症状速查、回归清单、交付说明 | 每轮验收时 |
| `references/infra.md` | GPU 机、SSM 隧道、显存、成本与停机 | 用云 GPU 时 |
| `scripts/scrollkit.py` | 可复用零件：Iso/Camera、DistrictField、SpritePicker、InkItem、DayNight、lantern_mask 等 | 写引擎时 import |
| `scripts/scroll_demo.py` | 最小可运行骨架（`stats` / `preview` / `render`） | 新项目第一步 |
| `scripts/comfy_client.py` | ComfyUI 提交/等待/拉回/合成透明精灵/上传/释放显存 | 生产素材 |
| `scripts/contact_sheet.py` | 素材对照表 | 每批新图 |
| `scripts/tex_tile.py` | 无缝贴图（bond/grid/soft/wrapx） | 做地面墙面 |
| `scripts/frame_grid.py` | 成片抽帧拼图、跟拍条 | 验收 |
| `scripts/popcheck.py` | 逐帧跳变检查 | 每次全片 |
| `scripts/sfx_mmaudio.py` | 配置驱动的音效流水线（plan/cut/run/tag/search/events/fetch/mix/auto） | 配音效 |
| `scripts/ssm_run.sh` | 经 AWS SSM 在 GPU 机上执行命令 | 用云 GPU |
| `assets/districts.qingming.json` | 《清》的功能团、档案、用途组、亲和度、地标时间轴 | 北宋/古城题材起点 |
| `assets/prompts.qingming.json` | 73 条精灵提示词 + 风格常量 + 编辑提示词 + 贴图提示词 | 写提示词时 |
| `assets/sfx_config.example.json` | 《清》的音效配置（场景、修正、标签、事件） | 配音效时 |

## 快速开始

```bash
python -m pip install numpy pillow imageio-ffmpeg               # 没有这三个就先装（系统自带的 python3 通常没有 numpy）

# S 指向本 skill 的 scripts 目录。按安装位置取其一：
#   Claude Code（个人）  S=~/.claude/skills/scroll-painting-animation/scripts
#   Claude Code（项目）  S=.claude/skills/scroll-painting-animation/scripts
#   Kiro                S=~/.kiro/skills/scroll-painting-animation/scripts
#   直接 clone 的仓库     S=<repo>/scripts
S=~/.claude/skills/scroll-painting-animation/scripts

python $S/scroll_demo.py stats                                  # 看功能团与选图是否合理
python $S/scroll_demo.py preview 2 7.6 10 12.6 --out /tmp/demo  # 白天 / 入夜 / 夜 / 黎明 各一帧
python $S/scroll_demo.py render --out /tmp/demo.mp4             # 14 秒小样（约 10 秒出完）
python $S/popcheck.py /tmp/demo.mp4 --skip 20
```

依赖：Python 3.10+、numpy、Pillow、imageio-ffmpeg（出片/抽帧）；中文字体（macOS 自带楷体，Linux 装 Noto CJK）。
GPU 部分（ComfyUI、MMAudio）在远端机器上，见 infra.md。
