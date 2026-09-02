#!/usr/bin/env python3
"""
统一后台服务 supervisor：用一个 launchd 项(com.papa.svc-supervisor)管理后台服务，
全部跑在统一 venv ~/.venvs/svc/bin/python。替代 launchd 的 KeepAlive/StartInterval/StartCalendarInterval。
"""
import os, sys, time, signal, subprocess, threading, datetime

HOME = os.path.expanduser("~")
PY = os.path.join(HOME, ".venvs/svc/bin/python")
PKB = os.path.join(HOME, "codebase/literature_maps/paper_kb")
N8N = os.path.join(HOME, "codebase/n8n")
PKLOG = os.path.join(HOME, "Library/Logs/paper-kb")
SUP_LOG = os.path.join(N8N, "logs/svc-supervisor.log")

PATH_ENV = "/Users/papa/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
RELAY_TOKEN = "0b792727c64ae18a498c96b80279928535034faf87397632138574f6c7bced7f"

TASKS = [
    # ---- 常驻 daemon (原 KeepAlive) ----
    dict(name="paperkb.server", kind="daemon",
         args=[PY, os.path.join(PKB, "kb_server.py"), "--port", "8787"],
         cwd=PKB, out=os.path.join(PKLOG, "server.log"), err=os.path.join(PKLOG, "server.log"), env={}),
    dict(name="wechat-text-relay", kind="daemon",
         args=[PY, os.path.join(N8N, "scripts/wechat-text-relay.py"),
               "--adapter-url", "http://127.0.0.1:18300/wechat-group-relay"],
         cwd=N8N, out=os.path.join(N8N, "logs/wechat-text-relay.log"),
         err=os.path.join(N8N, "logs/wechat-text-relay.err.log"),
         env={"WECHAT_GROUP_RELAY_ENDPOINT_TOKEN": RELAY_TOKEN}),
    dict(name="feishu-tunnel", kind="daemon",
         args=["ssh","-i",os.path.join(HOME,".ssh/feishu_relay_edge_ed25519"),
               "-o","BatchMode=yes","-o","IdentitiesOnly=yes","-o","ServerAliveInterval=15",
               "-o","ServerAliveCountMax=3","-o","ExitOnForwardFailure=yes","-o","StrictHostKeyChecking=accept-new",
               "-N","-L","127.0.0.1:18300:127.0.0.1:18300","root@47.114.113.152"],
         cwd=HOME, out=os.path.join(N8N,"logs/feishu-tunnel.log"),
         err=os.path.join(N8N,"logs/feishu-tunnel.log"), env={}),
    dict(name="wechat-image-relay", kind="daemon",
         args=[PY, os.path.join(N8N, "scripts/wechat-image-relay.py"),
               "--tag", "xiaolan", "--source-label", "微信小蓝炒股会媒体监控",
               "--chat-dir-id", "71345daa03ac00d81e0f824bb580d85e"],
         cwd=os.path.join(HOME, "codebase"),
         out=os.path.join(N8N, "logs/wechat-image-relay.log"),
         err=os.path.join(N8N, "logs/wechat-image-relay.err.log"), env={}),
    # 公众号监听与文字/图片监听共用同一个 svc venv 和 supervisor 生命周期。
    # 首次启动默认只建立游标，不回放已有历史；新消息由 manual-relay 入队。
    dict(name="wechat-biz-relay", kind="daemon",
         args=[PY, os.path.join(N8N, "scripts/wechat-biz-relay.py"), "--interval", "10"],
         cwd=N8N, out=os.path.join(N8N, "logs/wechat-biz-relay.log"),
         err=os.path.join(N8N, "logs/wechat-biz-relay.err.log"),
        env={"WECHAT_BIZ_RELAY_TAG": "quanneng",
             "WECHAT_BIZ_RELAY_TAG_MAP": '{"gh_926c397be7d3":"anqiang"}',
             "WECHAT_BIZ_FULLTEXT_ENABLED": "true",
             "WECHAT_BIZ_FULLTEXT_ACCOUNTS": "gh_926c397be7d3,gh_4ed145384731,gh_4ebab6e28beb,gh_5d502986b132",
             "WECHAT_BIZ_FULLTEXT_TIMEOUT_SECONDS": "15",
             "WECHAT_BIZ_FULLTEXT_RETRIES": "1",
             "WECHAT_BIZ_RELAY_ADAPTER_URL": "http://127.0.0.1:18300/manual-relay",
              "WECHAT_BIZ_RELAY_STATE": os.path.join(N8N, "state/wechat-biz-relay.json")}),
    # ---- 定时 interval (原 StartInterval, RunAtLoad) ----
    dict(name="paperkb.arxiv", kind="interval", interval=1800, run_at_load=True,
         args=[PY, os.path.join(PKB, "jobs.py"), "arxiv"],
         cwd=PKB, out=os.path.join(PKLOG, "arxiv.log"), err=os.path.join(PKLOG, "arxiv.log"),
         env={"PATH": PATH_ENV}),
    dict(name="paperkb.refresh", kind="interval", interval=120, run_at_load=True,
         args=[PY, os.path.join(PKB, "kb_refresh.py")],
         cwd=PKB, out=os.path.join(PKLOG, "refresh.log"), err=os.path.join(PKLOG, "refresh.log"), env={}),
    # ---- 观察池自动化:本地是唯一真源,edge 是扫描方 ----
    # sync 每 5 分钟推差异到 edge(diff 后才 PUT,避免无谓触发 45 天 hydration);
    # refresh 每 30 分钟触发,脚本内按沪时窗口+当日标记自行决定是否真正执行。
    dict(name="watchlist.sync", kind="interval", interval=300, run_at_load=True,
         args=["/bin/bash", os.path.join(N8N, "scripts/sync-watchlist-to-edge.sh")],
         cwd=N8N, out=os.path.join(N8N, "logs/watchlist-sync.log"),
         err=os.path.join(N8N, "logs/watchlist-sync.log"), env={"PATH": PATH_ENV}),
    dict(name="watchlist.refresh", kind="interval", interval=1800, run_at_load=False,
         args=[PY, os.path.join(N8N, "scripts/refresh-watchlist-from-proposals.py")],
         cwd=N8N, out=os.path.join(N8N, "logs/watchlist-refresh.log"),
         err=os.path.join(N8N, "logs/watchlist-refresh.log"), env={"PATH": PATH_ENV}),
    # ---- 周更 calendar (原 StartCalendarInterval: 周一 09:00) ----
    dict(name="paperkb.harvest", kind="calendar", weekday=1, hour=9, minute=0,
         args=[PY, os.path.join(PKB, "kb_harvest.py")],
         cwd=PKB, out=os.path.join(PKLOG, "harvest.log"), err=os.path.join(PKLOG, "harvest.log"),
         env={"PATH": PATH_ENV}),
]

_shutdown = threading.Event()
_procs = {}  # name -> Popen (daemons)

def slog(msg):
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} [supervisor] {msg}\n"
    try:
        with open(SUP_LOG, "a") as f: f.write(line)
    except OSError: pass
    sys.stderr.write(line)

def _open_out(t):
    outf = open(t["out"], "a")
    errf = outf if t["err"] == t["out"] else open(t["err"], "a")
    return outf, errf

def _child_env(t):
    e = dict(os.environ)
    e.update(t.get("env", {}))
    return e

def run_once(t):
    """跑一次并等待结束(interval/calendar)。"""
    outf, errf = _open_out(t)
    try:
        p = subprocess.Popen(t["args"], cwd=t["cwd"], env=_child_env(t), stdout=outf, stderr=errf)
        while p.poll() is None:
            if _shutdown.is_set():
                p.terminate()
                try: p.wait(10)
                except subprocess.TimeoutExpired: p.kill()
                break
            time.sleep(1)
        return p.returncode
    finally:
        outf.close()
        if errf is not outf: errf.close()

def daemon_loop(t):
    backoff = 1
    while not _shutdown.is_set():
        outf, errf = _open_out(t)
        try:
            slog(f"start daemon {t['name']}")
            p = subprocess.Popen(t["args"], cwd=t["cwd"], env=_child_env(t), stdout=outf, stderr=errf)
            _procs[t["name"]] = p
            while p.poll() is None:
                if _shutdown.is_set():
                    p.terminate()
                    try: p.wait(10)
                    except subprocess.TimeoutExpired: p.kill()
                    break
                time.sleep(1)
            rc = p.returncode
        finally:
            outf.close()
            if errf is not outf: errf.close()
        if _shutdown.is_set(): break
        slog(f"daemon {t['name']} exited rc={rc}; restart in {backoff}s")
        _shutdown.wait(backoff)
        backoff = min(backoff * 2, 60)  # 指数退避封顶60s
        # 正常存活重置退避
        if rc == 0: backoff = 1

def interval_loop(t):
    if t.get("run_at_load"): 
        if not _shutdown.is_set(): run_once(t)
    while not _shutdown.is_set():
        start = time.time()
        # 睡到下个周期(可被 shutdown 打断)
        while time.time() - start < t["interval"]:
            if _shutdown.is_set(): return
            time.sleep(min(5, t["interval"]))
        if not _shutdown.is_set(): run_once(t)

def _next_calendar(now, weekday, hour, minute):
    # launchd Weekday: 1=周一..7=周日,0=周日; 用 isoweekday(周一=1..周日=7)匹配(0视作7)
    target = 7 if weekday == 0 else weekday
    for d in range(0, 8):
        cand = (now + datetime.timedelta(days=d)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        if cand > now and cand.isoweekday() == target:
            return cand
    return now + datetime.timedelta(days=7)

def calendar_loop(t):
    while not _shutdown.is_set():
        now = datetime.datetime.now()
        nxt = _next_calendar(now, t["weekday"], t["hour"], t["minute"])
        slog(f"{t['name']} next run at {nxt:%Y-%m-%d %H:%M}")
        while datetime.datetime.now() < nxt:
            if _shutdown.is_set(): return
            time.sleep(20)
        if not _shutdown.is_set():
            slog(f"run calendar {t['name']}")
            run_once(t)
            time.sleep(61)  # 防同一分钟重复触发

def _handle_term(signum, frame):
    slog(f"got signal {signum}, shutting down")
    _shutdown.set()

def main():
    os.makedirs(PKLOG, exist_ok=True)
    os.makedirs(os.path.dirname(SUP_LOG), exist_ok=True)
    signal.signal(signal.SIGTERM, _handle_term)
    signal.signal(signal.SIGINT, _handle_term)
    slog(f"supervisor up, python={PY}, {len(TASKS)} tasks")
    threads = []
    for t in TASKS:
        fn = {"daemon": daemon_loop, "interval": interval_loop, "calendar": calendar_loop}[t["kind"]]
        th = threading.Thread(target=fn, args=(t,), name=t["name"], daemon=True)
        th.start(); threads.append(th)
    while not _shutdown.is_set():
        time.sleep(1)
    slog("waiting tasks to stop")
    for th in threads: th.join(timeout=15)
    slog("supervisor exit")

if __name__ == "__main__":
    main()
