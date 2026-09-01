#!/bin/bash
# 把 Tier1 基线依赖装进 ~/.venvs/svc 并逐个验证 import。
set -uo pipefail
PIP="$HOME/.venvs/svc/bin/pip"
PY="$HOME/.venvs/svc/bin/python"
LOG=$(mktemp)

PKGS=(akshare baostock efinance tushare yfinance stockstats exchange-calendars \
  anthropic openai litellm tiktoken langchain-core langchain-openai langgraph \
  fastapi uvicorn httpx pydantic-settings python-multipart sse-starlette slowapi aiofiles \
  sqlalchemy pymysql redis pymongo lark-oapi \
  python-dotenv tenacity schedule apscheduler pypinyin openpyxl json-repair orjson fire \
  questionary feedparser parsel pyjwt python-docx markdown tqdm pytz zstandard av)

echo "=================================================================="
echo " 安装 Tier1 基线 (${#PKGS[@]} 个) → ~/.venvs/svc"
echo "=================================================================="
echo "[1] 批量安装(共享依赖解析) ..."
"$PIP" install --upgrade "${PKGS[@]}" > "$LOG" 2>&1
echo "    批量 exit=$? (详见 $LOG)"

echo "[2] 逐包兜底(批量若漏装,单独补;已装则秒过) ..."
INSTALL_FAIL=()
for p in "${PKGS[@]}"; do
  "$PIP" install -q "$p" >> "$LOG" 2>&1 || INSTALL_FAIL+=("$p")
done
[ ${#INSTALL_FAIL[@]} -gt 0 ] && echo "    ❌ 安装失败: ${INSTALL_FAIL[*]}" || echo "    ✅ 全部安装成功"

echo "[3] 逐个 import 验证可用性 ..."
"$PY" - <<'PYEOF'
import importlib
# 安装名 -> 候选 import 名(任一成功即算可用)
M = {
 "akshare":["akshare"],"baostock":["baostock"],"efinance":["efinance"],"tushare":["tushare"],
 "yfinance":["yfinance"],"stockstats":["stockstats"],"exchange-calendars":["exchange_calendars"],
 "anthropic":["anthropic"],"openai":["openai"],"litellm":["litellm"],"tiktoken":["tiktoken"],
 "langchain-core":["langchain_core"],"langchain-openai":["langchain_openai"],"langgraph":["langgraph"],
 "fastapi":["fastapi"],"uvicorn":["uvicorn"],"httpx":["httpx"],"pydantic-settings":["pydantic_settings"],
 "python-multipart":["multipart","python_multipart"],"sse-starlette":["sse_starlette"],"slowapi":["slowapi"],
 "aiofiles":["aiofiles"],"sqlalchemy":["sqlalchemy"],"pymysql":["pymysql"],"redis":["redis"],"pymongo":["pymongo"],
 "lark-oapi":["lark_oapi"],"python-dotenv":["dotenv"],"tenacity":["tenacity"],"schedule":["schedule"],
 "apscheduler":["apscheduler"],"pypinyin":["pypinyin"],"openpyxl":["openpyxl"],"json-repair":["json_repair"],
 "orjson":["orjson"],"fire":["fire"],"questionary":["questionary"],"feedparser":["feedparser"],"parsel":["parsel"],
 "pyjwt":["jwt"],"python-docx":["docx"],"markdown":["markdown"],"tqdm":["tqdm"],"pytz":["pytz"],
 "zstandard":["zstandard"],"av":["av"],
}
ok=[]; bad=[]
for pkg,cands in M.items():
    done=False; err=""
    for name in cands:
        try: importlib.import_module(name); done=True; break
        except Exception as e: err=f"{name}: {type(e).__name__}: {e}"
    (ok if done else bad).append(pkg if done else (pkg,err))
print(f"\n  ✅ 可用 {len(ok)}/{len(M)}:")
print("     "+" ".join(ok))
if bad:
    print(f"\n  ❌ 不可用 {len(bad)}:")
    for pkg,e in bad: print(f"     {pkg:22s} {e[:70]}")
PYEOF
echo
echo "完成。svc 现有包总数: $("$PY" -m pip list 2>/dev/null | tail -n +3 | wc -l | tr -d ' ')"
