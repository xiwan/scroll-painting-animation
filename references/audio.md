# 音效：MMAudio 按画面生成、AST 核对、混音

工具：`scripts/sfx_mmaudio.py` + 一个 JSON 配置（`assets/sfx_config.example.json` 是《清》实际用过的）。

## 1. 选型

- 要的是**环境声和场景音效**，不是配乐。歌曲生成模型（YuE 之类）不适合。
- MMAudio（视频 + 文字条件，44.1kHz）：能"看画面"出声，节奏与画面同步。**权重 CC BY-NC 4.0，非商用**——
  商用片子要换别的来源，交付时必须告诉用户。
- 核对用 AudioSet 分类模型 AST（MIT/ast-finetuned-audioset），给每段打标签——我们听不到声音，只能这样验。

## 2. GPU 机上安装

```bash
mkdir -p /opt/mmaudio && cd /opt/mmaudio
git clone --depth 1 https://github.com/hkchengrex/MMAudio.git src
python3 -m venv venv && venv/bin/pip install -U pip
venv/bin/pip install torch==2.5.1 torchvision==0.20.1 torchaudio==2.5.1 --index-url https://download.pytorch.org/whl/cu121
cd src && ../venv/bin/pip install -e .     # 首次运行会自动下载权重（large_44k_v2 + 外部权重）
```

- 用独立 venv，别装进 ComfyUI 的环境（依赖会打架）。
- 读视频用 PyAV（自带 ffmpeg 库），GPU 机上不需要装 ffmpeg。
- AST 打标签可用 ComfyUI 的 venv（有 transformers）；那里的 torchaudio 读 flac 要 torchcodec，改用 PyAV 解码。
- 显存：large 约 6GB；ComfyUI 常驻会占 20GB，跑之前 POST `/free` 卸载模型。

## 3. 设计：环境声窗口 + 场景音效

- **环境声**：成片切成 10 秒窗、步长 8 秒（相邻重叠 2 秒），每窗按中心时刻取场景提示词（scenes 表）；
  每段一个增益（高潮 +3dB、夜里 −3dB）。
- **场景音效（events）**：在画面对应时刻叠上去的短事件——孩童嬉戏（放风筝/街上追跑）、城门吱呀打开、
  说书与掌声、黎明鸡鸣、结尾钟声（尾音拖到片尾淡出）。每条：名、起始秒、时长、提示词、增益、想要的标签。
- 提示词写具体的声源和动作，别写抽象氛围；负向提示里放 music/singing/modern vehicles。

## 4. 核对与挑选（典型翻车）

| 现象（AST 标签） | 原因 | 处理 |
|---|---|---|
| Music / Theme music 很高 | 画面"热闹"或"戏剧化"，模型配成了配乐 | 多出候选：一半带画面、一半纯文字（去掉画面特征），按"想要的标签 − 2×配乐类标签"打分挑 |
| Thunder / Rain | 提示词里有 "splashing hard" 之类 | 改提示词，去掉水花 |
| 几乎无声（所有标签 <0.1） | 画面太空 | 改提示词或换种子 |
| 雨声弱 | 只写了 light rain | 写 "steady rain pouring on leaves and a thatched roof" |
| 钟声被认成 Gong | 中国寺钟本来就接近 | 可接受；打分时别把 Tubular bells 当配乐扣分 |

`tag` 自动列出可疑窗口，`search` 对它们多出 6 个候选自动挑；`auto` 一条龙。

## 5. 混音

- 每窗先拉到同一 RMS，再乘段落增益；相邻窗用**正弦/余弦**交叉淡化（两段不相关的环境声叠加，总功率不塌；
  用 sin² 会在中间凹 3dB）。
- 事件同样归一后按增益叠上，头 0.4 秒淡入、尾 1.2 秒（钟声 2.5 秒）淡出。
- 首尾 1 秒淡入淡出与画面一致；整体拉到约 −21 dBFS RMS（不用 loudnorm 的动态模式——会把夜里和高潮压平），
  限幅 0.7（−3 dBFS），单声道复制成立体声。成片 −17 LUFS、真峰值约 −1 dBFS。
- **测峰值的陷阱**：把完全相同的左右声道用 `-ac 1` 下混成单声道再测，会多出 +3dB，看起来像削顶——
  用 ebur128 的 true peak 测。
- 并入：视频流 copy、AAC 192k、`-shortest`、`+faststart`。原无声片保留，便于重混。

## 6. 资产

生成好的每段 flac 留在项目里（`out_dir`），之后调增益/换某一段都不用再开 GPU；改了画面内容的段落才重做。
