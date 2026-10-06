#!/bin/zsh
# 在远端 GPU 机（EC2，经 AWS SSM，无需开 22 端口）上执行一段 shell 命令，等它跑完再打印标准输出/错误。
# 用法: SSM_INSTANCE=i-xxxx SSM_REGION=us-west-2 ssm_run.sh '<shell 命令>' [超时秒]
# 端口转发（本机 18188 → 远端 8188 的 ComfyUI）:
#   aws ssm start-session --region $SSM_REGION --target $SSM_INSTANCE \
#     --document-name AWS-StartPortForwardingSession --parameters '{"portNumber":["8188"],"localPortNumber":["18188"]}'
# 注意：SSM 会话闲置约 20 分钟会被断开，长时间不用后再用前先重开转发。
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
IID=${SSM_INSTANCE:?设置 SSM_INSTANCE}; R=${SSM_REGION:-us-west-2}
params=$(python3 -c 'import json,sys; print(json.dumps({"commands":[sys.argv[1]],"executionTimeout":[sys.argv[2]]}))' "$1" "${2:-600}")
cid=$(aws ssm send-command --region $R --instance-ids $IID --document-name AWS-RunShellScript \
      --parameters "$params" --query Command.CommandId --output text) || exit 1
while :; do
  st=$(aws ssm get-command-invocation --region $R --command-id $cid --instance-id $IID --query Status --output text 2>/dev/null)
  case $st in Success|Failed|TimedOut|Cancelled) break;; esac
  sleep 3
done
aws ssm get-command-invocation --region $R --command-id $cid --instance-id $IID --query StandardOutputContent --output text
err=$(aws ssm get-command-invocation --region $R --command-id $cid --instance-id $IID --query StandardErrorContent --output text)
[ -n "$err" ] && [ "$err" != "None" ] && echo "--- stderr ---" && echo "$err"
echo "[status: $st]"
