#!/bin/bash
# 一次性修复：给 WeChatRelay 签名的 python 写入 TCC 授权(AppData+FDA)，清理旧签名 python 进程并重启 relay。
# 依赖：python 已用 setup-relay-codesign.sh 签成 Authority=WeChatRelay。
#   bash scripts/fix-relay-fda.sh
set -uo pipefail

TCC="$HOME/Library/Application Support/com.apple.TCC/TCC.db"
PY_RES=$(ls -d /opt/homebrew/Cellar/python@3.14/*/Frameworks/Python.framework/Versions/3.14/Resources/Python.app/Contents/MacOS/Python 2>/dev/null | sort -V | tail -1)
VENV="/Users/papa/codebase/wechat-export-macos/.venv/bin/python"

echo "==================================================================="
echo " 修复 relay python 的 TCC 授权 + 清理旧进程"
echo "==================================================================="
echo "[*] 目标: $PY_RES"

# --- 0) 确认已是 WeChatRelay 签名 ---
SIGINFO=$(codesign -dvvv "$PY_RES" 2>&1)
if ! printf '%s' "$SIGINFO" | grep -qi "WeChatRelay"; then
  echo "[!] 未检测到 WeChatRelay 签名。当前 codesign 输出："
  printf '%s\n' "$SIGINFO" | grep -iE "Authority|Signature|adhoc|not signed" | sed 's/^/    /'
  echo "  若上面显示 adhoc / not signed，请先跑 setup-relay-codesign.sh；否则可注释本检查后重试。"
  exit 1
fi
echo "[=] 签名确认: WeChatRelay"

# --- 1) 生成当前签名二进制的 csreq blob ---
echo "[*] 生成 csreq ..."
CSREQ=$(/usr/bin/python3 - "$PY_RES" <<'PYEOF'
import ctypes, ctypes.util, binascii, sys
Sec=ctypes.CDLL(ctypes.util.find_library("Security"))
CF =ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
CF.CFURLCreateFromFileSystemRepresentation.restype=ctypes.c_void_p
CF.CFURLCreateFromFileSystemRepresentation.argtypes=[ctypes.c_void_p,ctypes.c_char_p,ctypes.c_long,ctypes.c_bool]
p=sys.argv[1].encode()
url=CF.CFURLCreateFromFileSystemRepresentation(None,p,len(p),False)
for name,restype,argc in [("SecStaticCodeCreateWithPath",ctypes.c_int,3),
                          ("SecCodeCopyDesignatedRequirement",ctypes.c_int,3),
                          ("SecRequirementCopyData",ctypes.c_int,3)]:
    getattr(Sec,name).restype=restype
code=ctypes.c_void_p()
Sec.SecStaticCodeCreateWithPath.argtypes=[ctypes.c_void_p,ctypes.c_uint32,ctypes.POINTER(ctypes.c_void_p)]
assert Sec.SecStaticCodeCreateWithPath(url,0,ctypes.byref(code))==0
req=ctypes.c_void_p()
Sec.SecCodeCopyDesignatedRequirement.argtypes=[ctypes.c_void_p,ctypes.c_uint32,ctypes.POINTER(ctypes.c_void_p)]
assert Sec.SecCodeCopyDesignatedRequirement(code,0,ctypes.byref(req))==0
data=ctypes.c_void_p()
Sec.SecRequirementCopyData.argtypes=[ctypes.c_void_p,ctypes.c_uint32,ctypes.POINTER(ctypes.c_void_p)]
assert Sec.SecRequirementCopyData(req,0,ctypes.byref(data))==0
CF.CFDataGetLength.restype=ctypes.c_long; CF.CFDataGetLength.argtypes=[ctypes.c_void_p]
CF.CFDataGetBytePtr.restype=ctypes.c_void_p; CF.CFDataGetBytePtr.argtypes=[ctypes.c_void_p]
n=CF.CFDataGetLength(data); ptr=CF.CFDataGetBytePtr(data)
print(binascii.hexlify(ctypes.string_at(ptr,n)).decode())
PYEOF
)
if [ -z "$CSREQ" ]; then echo "[!] csreq 生成失败"; exit 1; fi
echo "    csreq=${CSREQ:0:32}... (${#CSREQ} hex)"

# --- 2) 写入 AppData + FDA 授权(两个路径) ---
AUTH=$(sqlite3 "$TCC" "SELECT auth_value FROM access WHERE service='kTCCServiceSystemPolicyAppData' AND client='com.apple.Terminal' LIMIT 1;" 2>/dev/null)
[ -z "$AUTH" ] && AUTH=5
echo "[*] 写入授权 (auth_value=$AUTH) ..."
for SVC in kTCCServiceSystemPolicyAppData kTCCServiceSystemPolicyAllFiles; do
  for C in "$PY_RES" "$VENV"; do
    sqlite3 "$TCC" "INSERT OR REPLACE INTO access (service,client,client_type,auth_value,auth_reason,auth_version,csreq,indirect_object_identifier,flags,last_modified,boot_uuid,last_reminded) VALUES ('$SVC','$C',1,$AUTH,2,1,X'$CSREQ','UNUSED',0,strftime('%s','now'),'UNUSED',0);" \
      && echo "    ✅ $SVC <- $(basename "$C")" || echo "    ❌ 写入失败 $SVC $C"
  done
done
echo "[*] 重载 tccd ..."; killall tccd 2>/dev/null && echo "    tccd 已重载"

# --- 3) 清理所有旧签名 python3.14 进程；launchd 服务会以新签名自动/手动重启 ---
echo "[*] 结束旧 python3.14 进程(框架 Python) ..."
pkill -f "Python.app/Contents/MacOS/Python" 2>/dev/null && echo "    已发送结束信号" || echo "    (无匹配进程)"
sleep 2
U=$(id -u)
for L in com.papa.wechat-text-relay com.papa.wechat-image-relay com.papa.paperkb.server; do
  launchctl kickstart -k "gui/$U/$L" 2>/dev/null && echo "    ↻ 重启 $L"
done
sleep 3

# --- 4) 自检 ---
echo
echo "=== 自检 ==="
echo "[*] TCC 里 python 的 AppData/FDA 记录:"
sqlite3 "$TCC" "SELECT service,substr(client,-30),auth_value FROM access WHERE client LIKE '%Resources/Python.app%' OR client LIKE '%.venv/bin/python';" 2>/dev/null | sed 's/^/    /'
echo "[*] relay 进程与签名:"
for S in com.papa.wechat-text-relay com.papa.wechat-image-relay; do
  pid=$(launchctl list | grep "$S" | awk '{print $1}')
  exe=$(lsof -p "$pid" 2>/dev/null | awk '$4=="txt" && /MacOS\/Python/{print $NF; exit}')
  auth=$(codesign -dvvv "$exe" 2>&1 | grep '^Authority=' | head -1 | cut -d= -f2)
  echo "    $S pid=$pid authority=$auth"
done
echo "[*] 观察8秒 relay 是否有权限错误 ..."
b=$(grep -c "not permitted" /Users/papa/codebase/n8n/logs/wechat-text-relay.err.log 2>/dev/null)
sleep 8
a=$(grep -c "not permitted" /Users/papa/codebase/n8n/logs/wechat-text-relay.err.log 2>/dev/null)
echo "    权限错误累计: $b -> $a  $([ "$b" = "$a" ] && echo '✅ 无新增' || echo '⚠️ 有新增')"
echo
echo "完成。观察一两分钟是否还弹；若不弹即根治。"
echo "(若仍弹，说明此 macOS 拒绝手写 TCC 记录，需改用系统设置 GUI 添加 FDA)"
