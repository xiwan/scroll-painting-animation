# 基础设施：GPU 机、隧道、成本

## 0. 安全须知（先读）

- **这台 GPU 机当一次性实例用**：最小权限的 instance profile、不放长期凭据、用完就停。
  `ssm_run.sh` 走 `AWS-RunShellScript`，在 Linux 上**以 root 执行**，没有命令白名单。
- **配置文件和 workflow JSON 等同代码**：`sfx_config.json` 里的路径字段会进远端 shell，
  ComfyUI 的 `/prompt` 默认无鉴权、工作流能加载任意权重和自定义节点。只跑自己写的或审阅过的。
- **远端的 stdout/stderr 会原样打印**，也就是会进 AI agent 的上下文。别在远端跑 `env`、
  `cat ~/.aws/credentials` 之类会回显凭据的命令。
- **ComfyUI 绑在 127.0.0.1**，只经 SSM/SSH 端口转发访问，不要把 8188 暴露到公网。

## 1. GPU 机

- 一台 **24GB 显存级别**的 GPU（L4 / A10G / RTX 4090 都行）跑 ComfyUI（精灵、朝向编辑、贴图）和 MMAudio（音效）。
  AWS 上 `g6.xlarge`(L4) / `g5.xlarge`(A10G) 属于这一档；**价格按 region 和机型差别很大，查
  [EC2 定价页](https://aws.amazon.com/ec2/pricing/on-demand/)，别照抄任何文档里的数字**。
- ComfyUI 模型（实际用过的一套）：Z-Image Turbo bf16 + qwen_3_4b 文本编码 + ae VAE（文生图）、
  BiRefNet（抠图）、Qwen-Image-Edit-2511 Lightning 4 步（朝向编辑）、Flux（备用）。
- 显存一次只够一个大模型：ComfyUI 和 MMAudio 交替用，切换前 POST `/free`（`comfy_client.py free`）。

### `comfy_client.py` 内置工作流的前提

`t2i` 子命令里是写死的 14 节点 API 工作流，跑它之前远端要满足：

| 要求 | 具体 |
|---|---|
| 权重文件名 | `models/diffusion_models/z_image_turbo_bf16.safetensors`、`models/text_encoders/qwen_3_4b.safetensors`、`models/vae/ae.safetensors`、`models/BiRefNet/birefnet.safetensors`（文件名可用 `t2i_workflow(...)` 的参数换） |
| 节点 | `UNETLoader`/`CLIPLoader(type="lumina2")`/`ModelSamplingAuraFlow`/`EmptySD3LatentImage`（较新版 ComfyUI 核心节点）、`LoadBackgroundRemovalModel` + `RemoveBackground`（抠图节点包，装不上就把 11–14 号节点换成你那套抠图节点） |
| history 窗口 | `sprite` 子命令在 `/history?max_items=400` 里按 `gen/<name>_` 前缀找图：跑满 400 条或重启过 ComfyUI 就找不到，这时重出一次即可 |
| 换模型 | 不是 Z-Image 这套就别用 `t2i`，自己导出 API 格式 JSON 走 `run --workflow`（Z-Image 无负向提示，换成有负向的模型时提示词写法也要跟着改） |

**朝向编辑没有配套子命令**：`assets.md` §4 的 Qwen-Image-Edit 转身要自己导出 API JSON 走 `run --workflow`，
`comfy_client.py` 只有 `t2i / run / wait / sprite / upload / free`。

## 2. 访问（不开 22 端口）

本机需要的 IAM 权限（挂在你的 CLI 身份上）：

```
ssm:SendCommand, ssm:GetCommandInvocation, ssm:StartSession   # 资源限定到那一台实例
ec2:DescribeInstances, ec2:StopInstances                      # 查状态、停机
sts:GetCallerIdentity                                         # ssm_run.sh 回显身份用
```

实例侧要装 SSM Agent 并挂 `AmazonSSMManagedInstanceCore`（Amazon Linux / Ubuntu 官方 AMI 自带 Agent）。

- 执行命令：`SSM_INSTANCE=i-xxx SSM_REGION=us-west-2 scripts/ssm_run.sh '<命令>' [超时]`。
  **执行前会回显调用者身份和要执行的命令并要求确认**；无人值守（被脚本/agent 调用）时显式设 `SSM_RUN_YES=1`。
  `SSM_REGION` 未设时回退 `AWS_REGION`/`AWS_DEFAULT_REGION`，都没有就报错（不替你猜 region）。
- 访问 ComfyUI：SSM 端口转发，本机 18188 → 远端 8188：
  ```bash
  aws ssm start-session --region "$SSM_REGION" --target "$SSM_INSTANCE" \
    --document-name AWS-StartPortForwardingSession --parameters '{"portNumber":["8188"],"localPortNumber":["18188"]}'
  ```
  `comfy_client.py` 的 `--host` 默认就是 `http://127.0.0.1:18188`（和这条隧道对齐）；
  ComfyUI 跑在本机时用 `--host http://127.0.0.1:8188` 或设 `COMFY_HOST`。
- 会话**闲置约 20 分钟被断开**：长时间渲染后再用，先检查 `curl 127.0.0.1:18188/queue`，不通就重开。
- 下行慢时，合成/裁边/压缩放到服务器上做，只拉小文件（assets.md §3）。
- 临时脚本不要放 `/tmp`（macOS 会定期清理，多用户机器上还有符号链接抢占风险）——放项目或 skill 目录里。
  本仓库脚本的默认输出已经都在工作目录下，不在 `/tmp`。

## 3. 机器状态检查

```bash
scripts/ssm_run.sh 'uptime; nvidia-smi --query-gpu=name,memory.used --format=csv; ss -ltnp | grep 8188'
```

- 重启后 NVIDIA 驱动可能没加载（`nvidia-smi` 报错），ComfyUI 起不来——要重装/加载驱动、重启服务后才能用。
  机器"在运行"不等于"能用"。

## 4. 成本与停机

- 每个阶段结束都提醒用户：机器还开着、每小时多少钱（按实际机型/region 的定价）、已经开了多久。
- **停机、删除资源前必须征得用户同意**（素材、权重都在机器盘上；停机不丢 EBS，终止会丢）。
- 停机：`aws ec2 stop-instances --region <region> --instance-ids <id>`。

## 5. 不用云 GPU 也能做的部分

引擎、布局、运动、题字、昼夜、验收（第 3–7、9 步）全是本机 numpy + Pillow，不需要 GPU。
只有素材生产（第 2 步）和音效（第 8 步）要 GPU——素材可以先用 `scroll_demo.py` 里的占位色块跑通流程，
音效可以直接省掉或换成自己的素材库。
