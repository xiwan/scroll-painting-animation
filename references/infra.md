# 基础设施：GPU 机、隧道、成本

## 1. GPU 机

- 一台带 24GB 显存的 GPU（实际用 L4，us-west-2，约 1.3 美元/小时）跑 ComfyUI（精灵、朝向编辑、贴图）和 MMAudio（音效）。
- ComfyUI 模型（实际用过）：Z-Image Turbo bf16 + qwen_3_4b 文本编码 + ae VAE（文生图）、BiRefNet（抠图）、
  Qwen-Image-Edit-2511 Lightning 4 步（朝向编辑）、Flux（备用）。
- 显存一次只够一个大模型：ComfyUI 和 MMAudio 交替用，切换前 POST `/free`。

## 2. 访问（不开 22 端口）

- 执行命令：`scripts/ssm_run.sh '<命令>' [超时]`（AWS SSM send-command，设置 `SSM_INSTANCE`、`SSM_REGION`）。
- 访问 ComfyUI：SSM 端口转发，本机 18188 → 远端 8188：
  ```bash
  aws ssm start-session --region $SSM_REGION --target $SSM_INSTANCE \
    --document-name AWS-StartPortForwardingSession --parameters '{"portNumber":["8188"],"localPortNumber":["18188"]}'
  ```
- 会话**闲置约 20 分钟被断开**：长时间渲染后再用，先检查 `curl 127.0.0.1:18188/queue`，不通就重开。
- 下行慢时，合成/裁边/压缩放到服务器上做，只拉小文件（assets.md §3）。
- 本机临时脚本不要放 `/tmp`（macOS 会定期清理）——放项目或 skill 目录里。

## 3. 机器状态检查

```bash
ssm_run.sh 'uptime; nvidia-smi --query-gpu=name,memory.used --format=csv; ss -ltnp | grep 8188'
```

- 重启后 NVIDIA 驱动可能没加载（`nvidia-smi` 报错），ComfyUI 起不来——要重装/加载驱动、重启服务后才能用。
  机器"在运行"不等于"能用"。

## 4. 成本与停机

- 每个阶段结束都提醒用户：机器还开着、每小时多少钱、已经开了多久。
- **停机、删除资源前必须征得用户同意**（素材、权重都在机器盘上；停机不丢 EBS，终止会丢）。
- 停机：`aws ec2 stop-instances --region <region> --instance-ids <id>`。
