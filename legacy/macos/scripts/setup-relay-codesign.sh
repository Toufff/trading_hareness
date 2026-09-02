#!/bin/bash
# 给 relay 用的 Homebrew python 一个稳定的自签名身份，使其可被授予"完整磁盘访问"且永久生效。
# 幂等：首次配置、以及每次 brew 升级 python 后重签，都跑这一个脚本即可。
#
#   bash scripts/setup-relay-codesign.sh
#
# 之后在 系统设置 → 隐私与安全性 → 完全磁盘访问权限 里，把脚本末尾打印的那个二进制加进去并打开开关（仅首次需要）。
set -uo pipefail

CERT_NAME="WeChatRelay"
KEYCHAIN="$HOME/Library/Keychains/login.keychain-db"

echo "==================================================================="
echo " relay python 稳定签名配置 (cert: $CERT_NAME)"
echo "==================================================================="

# --- 1) 定位当前 Homebrew python3.14 的框架二进制(relay 实际运行的那个) ---
PY_RES=$(ls -d /opt/homebrew/Cellar/python@3.14/*/Frameworks/Python.framework/Versions/3.14/Resources/Python.app/Contents/MacOS/Python 2>/dev/null | sort -V | tail -1)
PY_BIN=$(ls -d /opt/homebrew/Cellar/python@3.14/*/Frameworks/Python.framework/Versions/3.14/bin/python3.14 2>/dev/null | sort -V | tail -1)
if [ -z "$PY_RES" ]; then echo "[!] 找不到 Homebrew python@3.14 框架二进制"; exit 1; fi
echo "[*] 目标二进制:"
echo "      $PY_RES"
[ -n "$PY_BIN" ] && echo "      $PY_BIN"

# --- 2) 若证书不存在则创建一张自签名代码签名证书 ---
if security find-identity -v -p codesigning "$KEYCHAIN" 2>/dev/null | grep -q "$CERT_NAME"; then
  echo "[=] 证书 '$CERT_NAME' 已存在，跳过创建"
else
  echo "[+] 创建自签名代码签名证书 '$CERT_NAME' ..."
  # 用 Homebrew 的 openssl(3.x, 支持 -legacy)；p12 必须用老式 MAC/PBE，否则 macOS security 不认
  OSSL=/opt/homebrew/opt/openssl@3/bin/openssl; [ -x "$OSSL" ] || OSSL=$(command -v openssl)
  echo "    openssl: $OSSL"
  WORK=$(mktemp -d)
  trap 'rm -rf "$WORK"' EXIT
  "$OSSL" req -x509 -newkey rsa:2048 -keyout "$WORK/key.pem" -out "$WORK/cert.pem" \
    -days 3650 -nodes -subj "/CN=$CERT_NAME" \
    -addext "basicConstraints=critical,CA:FALSE" \
    -addext "keyUsage=critical,digitalSignature" \
    -addext "extendedKeyUsage=critical,codeSigning" 2>/dev/null
  # -legacy + 老式 PBE/MAC，保证 macOS security import 能验证
  "$OSSL" pkcs12 -export -legacy -inkey "$WORK/key.pem" -in "$WORK/cert.pem" \
    -out "$WORK/id.p12" -passout pass:relay -name "$CERT_NAME" \
    -certpbe PBE-SHA1-3DES -keypbe PBE-SHA1-3DES -macalg sha1 2>/dev/null \
    || "$OSSL" pkcs12 -export -legacy -inkey "$WORK/key.pem" -in "$WORK/cert.pem" \
         -out "$WORK/id.p12" -passout pass:relay -name "$CERT_NAME" 2>/dev/null
  # 导入私钥+证书；-A 允许任意程序使用该私钥(避免 codesign 时反复弹钥匙串)
  security import "$WORK/id.p12" -k "$KEYCHAIN" -P relay -A -T /usr/bin/codesign \
    || { echo "[!] 导入失败"; exit 1; }
  # 将证书设为对"代码签名"可信(用户级信任，可能弹一次钥匙串授权)
  security add-trusted-cert -r trustRoot -p codeSign -k "$KEYCHAIN" "$WORK/cert.pem" 2>/dev/null \
    || echo "[!] add-trusted-cert 未成功(通常不影响签名，可忽略)"
  # 校验身份确实可用于代码签名
  if security find-identity -v -p codesigning "$KEYCHAIN" 2>/dev/null | grep -q "$CERT_NAME"; then
    echo "[+] 证书已创建并可用于签名"
  else
    echo "[!] 证书导入后仍未成为有效签名身份，终止"; exit 1
  fi
fi

# --- 3) 用该证书签名(保留 entitlements 如 allow-jit，生成新的证书型 designated requirement) ---
sign_one() {
  local f="$1"
  echo "[*] 签名: $f"
  codesign --force --sign "$CERT_NAME" --preserve-metadata=entitlements --timestamp=none "$f" \
    && echo "    ✅ 已签" || { echo "    ❌ 签名失败"; return 1; }
}
sign_one "$PY_RES"
[ -n "$PY_BIN" ] && sign_one "$PY_BIN"

# --- 4) 显示新身份 + 校验可运行 ---
echo
echo "[*] 新签名身份:"
codesign -dvvv "$PY_RES" 2>&1 | grep -iE "Authority|Identifier|CDHash|TeamIdentifier" | sed 's/^/      /'
echo "[*] 验证 python 仍可运行:"
"$PY_RES" -c "print('      python OK')" 2>&1 || echo "      ❌ python 无法运行(需排查签名)"

echo
echo "==================================================================="
echo " 首次配置：把下面这个二进制加入『完全磁盘访问权限』并打开开关"
echo "   $PY_RES"
echo " 之后重启 relay 即永久生效；brew 升级 python 后重跑本脚本重签即可。"
echo "==================================================================="
