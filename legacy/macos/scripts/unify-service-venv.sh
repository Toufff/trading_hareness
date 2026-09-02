#!/bin/bash
# 把 6 个 python 后台服务统一到一个 Homebrew-3.14 venv (~/.venvs/svc, WeChatRelay 身份)。
# 安全阀：新 venv 依赖装好且冒烟测试通过后，才改 plist；否则保持原状。原 plist 会备份。
set -uo pipefail

SVC_VENV="$HOME/.venvs/svc"
HB_PY="/opt/homebrew/bin/python3"
NEW_PY="$SVC_VENV/bin/python"
SRC_VENV="$HOME/codebase/.venv"          # paperkb 重依赖来源(3.12)
BK="$HOME/Library/LaunchAgents/_plist_backup_$(date +%Y%m%d_%H%M%S)"
SERVICES=(paperkb.arxiv paperkb.refresh paperkb.server paperkb.harvest wechat-text-relay wechat-image-relay)

echo "=================================================================="
echo " 统一服务 venv → $SVC_VENV"
echo "=================================================================="

# --- A) 建 venv(基于 Homebrew 3.14, 继承 WeChatRelay 身份) ---
echo "[A] 建 venv ..."
"$HB_PY" -V
mkdir -p "$HOME/.venvs"
"$HB_PY" -m venv "$SVC_VENV" || { echo "[!] venv 创建失败"; exit 1; }
"$NEW_PY" -m pip install --quiet --upgrade pip 2>&1 | tail -1
echo "    新解释器: $("$NEW_PY" -V 2>&1)  base=$(readlink -f "$NEW_PY")"

# --- B) 装依赖: paperkb 重栈(来自 3.12 freeze) + pycryptodome(relay) ---
echo "[B] 收集并安装依赖(可能现编译, 耗时数分钟) ..."
REQ=$(mktemp)
"$SRC_VENV/bin/python" -m pip freeze 2>/dev/null | grep -viE "^(pip|setuptools|wheel)==" > "$REQ"
echo "    源依赖 $(wc -l < "$REQ") 个 + pycryptodome"
echo "pycryptodome" >> "$REQ"
INSTALL_LOG=$(mktemp)
"$NEW_PY" -m pip install -r "$REQ" > "$INSTALL_LOG" 2>&1
PIP_RC=$?
if [ $PIP_RC -ne 0 ]; then
  echo "[!] 部分依赖安装失败(下面是错误摘要)："
  grep -iE "ERROR:|Could not build|no matching distribution|failed building" "$INSTALL_LOG" | head -15 | sed 's/^/    /'
  echo "    完整日志: $INSTALL_LOG"
  echo "    ⚠️ 依赖未装全 → 不改 plist, 服务保持原状。请把上面错误发我处理。"
  exit 2
fi
echo "    ✅ 依赖安装完成($("$NEW_PY" -m pip list 2>/dev/null | wc -l | tr -d ' ') 个包)"

# --- C) 冒烟测试关键 import ---
echo "[C] 冒烟测试关键模块 ..."
"$NEW_PY" - <<'PYSMOKE'
import importlib, sys
mods = ["Crypto.Cipher.AES","numpy","pandas","lxml","bs4","PIL"]
bad=[]
for m in mods:
    try: importlib.import_module(m)
    except Exception as e: bad.append(f"{m}: {e}")
if bad:
    print("  ❌ 导入失败:"); [print("    "+b) for b in bad]; sys.exit(1)
print("  ✅ 关键模块导入正常:", ", ".join(mods))
PYSMOKE
[ $? -ne 0 ] && { echo "    ⚠️ 冒烟测试失败 → 不改 plist。"; exit 3; }

# --- D) 备份并改 6 个 plist 指向新 python ---
echo "[D] 备份并改写 plist -> $NEW_PY"
mkdir -p "$BK"
for s in "${SERVICES[@]}"; do
  P="$HOME/Library/LaunchAgents/com.papa.$s.plist"
  [ -f "$P" ] || { echo "    (跳过 $s: 无 plist)"; continue; }
  cp "$P" "$BK/"
  cur=$(/usr/libexec/PlistBuddy -c "Print :ProgramArguments:0" "$P" 2>/dev/null)
  if echo "$cur" | grep -qi python; then
    /usr/libexec/PlistBuddy -c "Set :ProgramArguments:0 $NEW_PY" "$P" && echo "    ✅ $s: $cur -> $NEW_PY"
  else
    echo "    (跳过 $s: ProgramArguments:0 非 python = $cur)"
  fi
done
echo "    原 plist 已备份到: $BK"

# --- E) 重载 6 个服务(bootout+bootstrap 才能吃到新路径) ---
echo "[E] 重载服务 ..."
U=$(id -u)
for s in "${SERVICES[@]}"; do
  P="$HOME/Library/LaunchAgents/com.papa.$s.plist"; [ -f "$P" ] || continue
  launchctl bootout "gui/$U/com.papa.$s" 2>/dev/null
  launchctl bootstrap "gui/$U" "$P" 2>/dev/null && echo "    ↻ $s"
done
sleep 4

# --- F) 验证 ---
echo "[F] 验证进程与日志 ..."
for s in "${SERVICES[@]}"; do
  pid=$(launchctl list | grep "com.papa.$s" | awk '{print $1}')
  echo "    $s -> PID=${pid:-未运行}"
done
echo
echo "完成。相关服务已统一到 $NEW_PY"
echo "回滚: 将 $BK/*.plist 复制回 ~/Library/LaunchAgents 并 bootout+bootstrap。"
