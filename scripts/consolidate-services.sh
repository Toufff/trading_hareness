#!/bin/bash
# 把原 6 个 LaunchAgent 合并为 1 个 supervisor(com.papa.svc-supervisor)。
# 顺序：先停旧6个(避免与supervisor双实例/端口冲突)→删旧plist→起supervisor→验证。最后删 codebase/.venv。
set -uo pipefail
U=$(id -u)
LA="$HOME/Library/LaunchAgents"
OLD=(paperkb.server paperkb.arxiv paperkb.refresh paperkb.harvest wechat-text-relay wechat-image-relay)
BK="$LA/_consolidate_backup_$(date +%Y%m%d_%H%M%S)"; mkdir -p "$BK"

echo "=================================================================="
echo " 合并 6 个服务 → 1 个 supervisor"
echo "=================================================================="

echo "[1] 停止旧的 6 个 LaunchAgent ..."
for s in "${OLD[@]}"; do
  launchctl bootout "gui/$U/com.papa.$s" 2>/dev/null && echo "    ⏹ $s" || echo "    (·$s 已不在)"
done
sleep 2

echo "[2] 备份并移除旧 plist ..."
for s in "${OLD[@]}"; do
  P="$LA/com.papa.$s.plist"
  [ -f "$P" ] && { cp "$P" "$BK/"; rm -f "$P"; echo "    🗑 $s (已备份)"; }
done
echo "    备份目录: $BK"

echo "[3] 启动 supervisor ..."
launchctl bootout "gui/$U/com.papa.svc-supervisor" 2>/dev/null
launchctl bootstrap "gui/$U" "$LA/com.papa.svc-supervisor.plist" && echo "    ▶ supervisor 已 bootstrap" || echo "    ❌ bootstrap 失败"
sleep 6

echo "[4] 验证 ..."
sp=$(launchctl list | grep "com.papa.svc-supervisor" | awk '{print $1}')
echo "    supervisor PID=${sp:-未运行}"
echo "    子任务(常驻)进程:"
for pat in "kb_server.py" "wechat-text-relay.py" "wechat-image-relay.py"; do
  pid=$(pgrep -f "$pat" | head -1)
  echo "      $pat -> ${pid:-未运行}"
done
echo "    supervisor 日志尾部:"
tail -8 "$HOME/codebase/n8n/logs/svc-supervisor.log" 2>/dev/null | sed 's/^/      /'
echo "    登录项 python 条目数(应从6→1, 剩 supervisor):"
sfltool dumpbtm 2>/dev/null | grep -c "Name: python" | awk '{print "      python 名条目:",$1}'

echo "[5] 删除已冗余的 codebase/.venv(先存 freeze 备份) ..."
CB="$HOME/codebase/.venv"
if [ -d "$CB" ]; then
  "$CB/bin/python" -m pip freeze > "$HOME/codebase/.venv-freeze-$(date +%Y%m%d).txt" 2>/dev/null && echo "    freeze 备份: ~/codebase/.venv-freeze-$(date +%Y%m%d).txt"
  rm -rf "$CB" && echo "    🗑 已删 codebase/.venv"
else
  echo "    (codebase/.venv 不存在,跳过)"
fi

echo
echo "完成。现在只有 1 个后台项 com.papa.svc-supervisor 管理全部 6 个任务。"
echo "回滚：把 $BK/*.plist 复制回 $LA 并 bootstrap，同时 bootout supervisor。"
