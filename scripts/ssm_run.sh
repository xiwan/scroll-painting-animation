#!/usr/bin/env bash
# 在远端 GPU 机（EC2，经 AWS SSM，无需开 22 端口）上执行一段 shell 命令，等它跑完再打印标准输出/错误。
#
# ⚠ 这个脚本把任意字符串发到远端执行。AWS-RunShellScript 在 Linux 上**以 root 身份**运行，
#   没有命令白名单。请把 GPU 机当作一次性实例：最小权限的 instance profile、不放长期凭据、
#   用完就停。远端的 stdout/stderr 会原样打印到终端（也就是会进 AI agent 的上下文），
#   所以别在远端执行会回显凭据的命令（env、cat ~/.aws/credentials 之类）。
#
# 用法: SSM_INSTANCE=i-xxxx SSM_REGION=us-west-2 ssm_run.sh '<shell 命令>' [超时秒]
#   SSM_REGION   未设时回退 AWS_REGION / AWS_DEFAULT_REGION，都没有就报错（不猜 region）
#   SSM_RUN_YES=1 跳过执行前的确认（无人值守/被 agent 调用时需要显式设置）
#
# 端口转发（本机 18188 → 远端 8188 的 ComfyUI）:
#   aws ssm start-session --region "$SSM_REGION" --target "$SSM_INSTANCE" \
#     --document-name AWS-StartPortForwardingSession --parameters '{"portNumber":["8188"],"localPortNumber":["18188"]}'
# 注意：SSM 会话闲置约 20 分钟会被断开，长时间不用后再用前先重开转发。
set -euo pipefail

IID=${SSM_INSTANCE:?设置 SSM_INSTANCE（形如 i-0123456789abcdef0）}
R=${SSM_REGION:-${AWS_REGION:-${AWS_DEFAULT_REGION:-}}}
[ -n "$R" ] || { echo "设置 SSM_REGION（或 AWS_REGION）" >&2; exit 2; }
[[ $IID =~ ^i-[0-9a-f]+$ ]] || { echo "SSM_INSTANCE 格式不对：$IID" >&2; exit 2; }
[[ $R =~ ^[a-z0-9-]+$ ]] || { echo "SSM_REGION 格式不对：$R" >&2; exit 2; }
[ $# -ge 1 ] || { echo "用法: ssm_run.sh '<shell 命令>' [超时秒]" >&2; exit 2; }

# 先说清楚用的是谁的身份、要动哪台机器，再执行。
WHO=$(aws sts get-caller-identity --query Arn --output text 2>/dev/null || echo "(取不到调用者身份)")
if [ "${SSM_RUN_YES:-}" != "1" ]; then
  { echo "即将在远端以 root 执行命令"; echo "  实例: $IID  region: $R"; echo "  身份: $WHO"
    echo "  命令: $1"; printf "  继续？[y/N] "; } >&2
  ans=
  if [ -r /dev/tty ]; then read -r ans </dev/tty 2>/dev/null || ans=; fi
  case $ans in
    y|Y|yes|YES) ;;
    "") echo >&2; echo "无法读取确认（非交互环境）。确认要让调用方（脚本 / AI agent）直接在远端以 root 执行命令时，" >&2
        echo "显式设置 SSM_RUN_YES=1，例如配置里写 runner: [\"/usr/bin/env\",\"SSM_RUN_YES=1\",\"<此脚本>\",\"{cmd}\",\"{timeout}\"]" >&2
        exit 2;;
    *) echo "已取消" >&2; exit 130;;
  esac
else
  echo "[ssm_run] $IID @ $R as $WHO" >&2
fi

params=$(python3 -c 'import json,sys; print(json.dumps({"commands":[sys.argv[1]],"executionTimeout":[sys.argv[2]]}))' "$1" "${2:-600}")
cid=$(aws ssm send-command --region "$R" --instance-ids "$IID" --document-name AWS-RunShellScript \
      --parameters "$params" --query Command.CommandId --output text)
while :; do
  st=$(aws ssm get-command-invocation --region "$R" --command-id "$cid" --instance-id "$IID" \
       --query Status --output text 2>/dev/null || echo Pending)
  case $st in Success|Failed|TimedOut|Cancelled) break;; esac
  sleep 3
done
aws ssm get-command-invocation --region "$R" --command-id "$cid" --instance-id "$IID" \
  --query StandardOutputContent --output text
err=$(aws ssm get-command-invocation --region "$R" --command-id "$cid" --instance-id "$IID" \
      --query StandardErrorContent --output text)
if [ -n "$err" ] && [ "$err" != "None" ]; then echo "--- stderr ---"; echo "$err"; fi
echo "[status: $st]"
