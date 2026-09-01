"""Weekly analyst review, 2026-08-24 to 2026-08-28.

Follows the point-in-time boundary the archive documents:

  strategy_available_at / available_at  is the only clock that may score.
  stated_at / published_at              is replay evidence, never a hit rate.

Everything printed under "可计分" respects that. Everything under "作者时间"
does not, and is labelled so it cannot be read as a hit rate by mistake.

Run:
  docker compose exec -T -e PYTHONPATH=/app quant-research python < this_file
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import statistics
import traceback

from app.main import db

CN = ZoneInfo("Asia/Shanghai")
WEEK_START, WEEK_END = date(2026, 8, 24), date(2026, 8, 28)
INDICES = {"000001.SH": "上证综指", "399001.SZ": "深证成指", "399006.SZ": "创业板指"}


def section(title):
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def guard(title, fn):
    section(title)
    try:
        fn()
    except Exception:
        print("  [本节失败]")
        print("  " + "\n  ".join(traceback.format_exc().splitlines()[-4:]))


def analyst_labels(c):
    """Display names when the archive stores them, ids otherwise."""
    try:
        rows = c.execute("SELECT * FROM quant.remote_analysts").fetchall()
    except Exception:
        return {}
    labels = {}
    for row in rows:
        data = dict(row)
        key = data.get("remote_analyst_id")
        for column in ("display_name", "name", "nickname", "label", "title"):
            if data.get(column):
                labels[key] = str(data[column])
                break
    return labels


with db.transaction() as c:
    NAMES = analyst_labels(c)
    label = lambda key: NAMES.get(key, key)

    def trading_days():
        rows = c.execute(
            """SELECT DISTINCT trading_date FROM quant.canonical_bars_daily
                WHERE trading_date BETWEEN %s AND %s AND volume > 0
                ORDER BY trading_date""", (WEEK_START, WEEK_END)).fetchall()
        return [r["trading_date"] for r in rows]

    DAYS = trading_days()

    # ---------------------------------------------------------------- market
    def market():
        print(f"本周交易日: {', '.join(str(d) for d in DAYS)}  ({len(DAYS)} 天)\n")
        if not DAYS:
            print("  本周无日线数据，后续全部无法结算")
            return
        first, last = DAYS[0], DAYS[-1]
        print(f"{'指数':12}{'周初收盘':>12}{'周末收盘':>12}{'周变化':>10}")
        for symbol, name in INDICES.items():
            row = c.execute(
                """SELECT max(close) FILTER (WHERE trading_date=%s) a,
                          max(close) FILTER (WHERE trading_date=%s) b
                     FROM quant.canonical_bars_daily WHERE symbol=%s""",
                (first, last, symbol)).fetchone()
            if row and row["a"] and row["b"]:
                a, b = float(row["a"]), float(row["b"])
                print(f"{name:12}{a:>12.2f}{b:>12.2f}{(b/a-1)*100:>+9.2f}%")
            else:
                print(f"{name:12}{'缺日线':>12}")
        print()
        print(f"{'日期':12}{'涨':>7}{'跌':>7}{'平':>7}{'等权中位':>10}{'封板':>7}")
        for day in DAYS:
            row = c.execute(
                """SELECT count(*) FILTER (WHERE close>pre_close) up,
                          count(*) FILTER (WHERE close<pre_close) down,
                          count(*) FILTER (WHERE close=pre_close) flat,
                          percentile_cont(0.5) WITHIN GROUP (
                            ORDER BY (close/nullif(pre_close,0)-1)*100) med,
                          count(*) FILTER (WHERE limit_up IS NOT NULL
                                             AND close>=limit_up-0.005) sealed
                     FROM quant.canonical_bars_daily b
                     JOIN quant.instruments i USING(symbol)
                    WHERE b.trading_date=%s AND b.volume>0 AND i.list_date IS NOT NULL""",
                (day,)).fetchone()
            med = float(row["med"]) if row["med"] is not None else 0.0
            print(f"{str(day):12}{row['up']:>7}{row['down']:>7}{row['flat']:>7}"
                  f"{med:>+9.2f}%{row['sealed']:>7}")

    guard("一、本周市场实际表现", market)

    # -------------------------------------------------------------- coverage
    def coverage():
        rows = c.execute(
            """SELECT remote_analyst_id a, count(*) n,
                      min(received_at AT TIME ZONE 'Asia/Shanghai') first,
                      max(received_at AT TIME ZONE 'Asia/Shanghai') last
                 FROM quant.remote_analyst_messages
                WHERE (received_at AT TIME ZONE 'Asia/Shanghai')::date BETWEEN %s AND %s
                GROUP BY 1 ORDER BY 2 DESC""", (WEEK_START, WEEK_END)).fetchall()
        print(f"{'分析师':22}{'本周消息':>9}  最早 → 最晚 (本地接收时间)")
        for r in rows:
            print(f"{label(r['a'])[:20]:22}{r['n']:>9}  {str(r['first'])[:16]} → {str(r['last'])[:16]}")
        if not rows:
            print("  本周没有任何消息落库 —— 先查同步是否在跑")
        print()
        obs = c.execute(
            """SELECT analyst_id a, scope, status, count(*) n
                 FROM quant.analyst_observations
                WHERE (strategy_available_at AT TIME ZONE 'Asia/Shanghai')::date BETWEEN %s AND %s
                GROUP BY 1,2,3 ORDER BY 1,2""", (WEEK_START, WEEK_END)).fetchall()
        if obs:
            print(f"{'分析师':22}{'范围':8}{'状态':14}{'条数':>6}")
            for r in obs:
                print(f"{label(r['a'])[:20]:22}{r['scope']:8}{r['status']:14}{r['n']:>6}")
        else:
            print("  本周没有抽取出观点 —— 抽取流水线可能未运行")

    guard("二、证据覆盖（按本地可用时间，不是作者标注时间）", coverage)

    # -------------------------------------------------------- PIT settlement
    def settled():
        # quant.outcomes is what recompute_outcomes writes, keyed on claim_id.
        # A claim with no row there is simply not settleable yet - its exit bar
        # does not exist, or its entry session opened locked / suspended and was
        # deliberately left unsettled rather than credited as a fillable order.
        counts = c.execute(
            """SELECT c.remote_analyst_id a, c.horizon_days h,
                      count(*) claims,
                      count(o.claim_id) settled
                 FROM quant.analyst_claims c
                 LEFT JOIN quant.outcomes o ON o.claim_id = c.claim_id
                WHERE c.scope='stock' AND c.direction<>0
                  AND (c.available_at AT TIME ZONE 'Asia/Shanghai')::date BETWEEN %s AND %s
                GROUP BY 1,2 ORDER BY 1,2""", (WEEK_START, WEEK_END)).fetchall()
        if not counts:
            print("  本周没有可结算范围内的个股观点（scope=stock 且 direction<>0）")
        else:
            print(f"{'分析师':22}{'周期':>6}{'观点数':>8}{'已结算':>8}{'待结算':>8}")
            for r in counts:
                print(f"{label(r['a'])[:20]:22}{r['h'] or 0:>6}{r['claims']:>8}"
                      f"{r['settled']:>8}{r['claims']-r['settled']:>8}")
        print()
        mat = c.execute(
            """SELECT c.remote_analyst_id a, o.horizon_days h, count(*) n,
                      count(*) FILTER (WHERE o.raw_return * c.direction > 0) hit,
                      round(avg(o.raw_return)::numeric * 100, 3) raw,
                      round(avg(o.excess_return)::numeric * 100, 3) excess,
                      round(avg(o.maximum_adverse_excursion)::numeric * 100, 3) mae
                 FROM quant.analyst_claims c
                 JOIN quant.outcomes o ON o.claim_id = c.claim_id
                WHERE (c.available_at AT TIME ZONE 'Asia/Shanghai')::date BETWEEN %s AND %s
                GROUP BY 1,2 ORDER BY 3 DESC""", (WEEK_START, WEEK_END)).fetchall()
        if not mat:
            print("  ★ 本周 0 条已结算 —— 无法给任何分析师算命中率")
            print("    这本身就是结论：多数观点周期长于本周剩余交易日，")
            print("    且入场被限定为次日开盘、排除一字/停牌，不可成交的不予记账。")
            return
        print(f"{'分析师':20}{'周期':>5}{'n':>5}{'方向命中':>9}{'原始':>9}{'超额沪深300':>12}{'最大逆行':>10}")
        for r in mat:
            rate = r["hit"] / r["n"] * 100 if r["n"] else 0
            get = lambda k: float(r[k]) if r[k] is not None else 0.0
            flag = "" if r["n"] >= 20 else "  ← 样本不足，不可据此调权"
            print(f"{label(r['a'])[:18]:20}{r['h'] or 0:>5}{r['n']:>5}{rate:>8.0f}%"
                  f"{get('raw'):>+8.2f}%{get('excess'):>+11.2f}%{get('mae'):>+9.2f}%{flag}")

    def market_scope():
        rows = c.execute(
            """SELECT c.remote_analyst_id a, c.horizon_days h, c.direction, count(*) n,
                      min((c.available_at AT TIME ZONE 'Asia/Shanghai')) first
                 FROM quant.analyst_claims c
                WHERE c.scope='market'
                  AND (c.available_at AT TIME ZONE 'Asia/Shanghai')::date BETWEEN %s AND %s
                GROUP BY 1,2,3 ORDER BY 1,2""", (WEEK_START, WEEK_END)).fetchall()
        if not rows:
            print("  本周无市场方向观点")
            return
        print("  市场观点没有自动结算表；下面给出方向与本周实际指数变化的对照材料。")
        print(f"\n{'分析师':22}{'周期':>6}{'方向':>6}{'条数':>6}  首次可用")
        for r in rows:
            arrow = {1: "看多", -1: "看空", 0: "中性"}.get(int(r["direction"]), "?")
            print(f"{label(r['a'])[:20]:22}{r['h'] or 0:>6}{arrow:>6}{r['n']:>6}  {str(r['first'])[:16]}")

    guard("三、可计分层：个股观点 PIT 结算（唯一能进胜率的层）", settled)
    guard("三之二、市场方向观点（与上面的指数变化对照读）", market_scope)

    # ------------------------------------------------------- author-time view
    def replay():
        rows = c.execute(
            """SELECT analyst_id a, scope, subject_label, action, direction, horizon_days h,
                      (strategy_available_at AT TIME ZONE 'Asia/Shanghai') avail,
                      (stated_at AT TIME ZONE 'Asia/Shanghai') stated,
                      left(evidence_span, 60) span
                 FROM quant.analyst_observations
                WHERE (strategy_available_at AT TIME ZONE 'Asia/Shanghai')::date BETWEEN %s AND %s
                  AND status='eligible' AND scope IN ('market','stock')
                ORDER BY analyst_id, strategy_available_at LIMIT 60""",
            (WEEK_START, WEEK_END)).fetchall()
        if not rows:
            print("  无 eligible 观点")
            return
        print("  仅供理解判断，不计入命中率。延迟 = 本地可用时间 − 作者标注时间。\n")
        print(f"{'分析师':16}{'可用时刻':>17}{'延迟':>9}{'范围':7}{'方向':>5}{'周期':>5}  标的/摘要")
        for r in rows:
            lag = ""
            if r["stated"] and r["avail"]:
                minutes = (r["avail"] - r["stated"]).total_seconds() / 60
                lag = f"{minutes:>+7.0f}分"
            print(f"{label(r['a'])[:14]:16}{str(r['avail'])[:16]:>17}{lag:>9}"
                  f"{r['scope']:7}{r['direction']:>5}{r['h'] or 0:>5}  "
                  f"{(r['subject_label'] or r['span'] or '')[:36]}")

    guard("四、作者时间复盘（不计入胜率）", replay)

    # ------------------------------------------------------------- scorecard
    def scorecards():
        rows = c.execute(
            """SELECT * FROM quant.analyst_skill_profiles ORDER BY 1 LIMIT 20""").fetchall()
        if not rows:
            print("  无技能画像")
            return
        for r in rows:
            data = {k: v for k, v in dict(r).items() if v is not None}
            print("  " + str(data)[:200])

    guard("五、既有技能画像 / 权重", scorecards)

    def freshness():
        # The daily pipeline is what settles analyst claims, and until
        # 2026-08-27 it died in its sync stage at ~851s and never reached
        # recompute_outcomes. There was also no scheduler. So an empty week is
        # at least as likely to mean "settlement never ran" as "no claims
        # matured", and the two must not be confused.
        row = c.execute(
            """SELECT count(*) n, max(calculated_at AT TIME ZONE 'Asia/Shanghai') last,
                      max(entry_date) last_entry
                 FROM quant.outcomes WHERE claim_id IS NOT NULL""").fetchone()
        print(f"  quant.outcomes(claim): {row['n']} 行，最后计算于 {row['last']}，最后入场日 {row['last_entry']}")
        rows = c.execute(
            """SELECT (calculated_at AT TIME ZONE 'Asia/Shanghai')::date d, count(*) n
                 FROM quant.outcomes WHERE claim_id IS NOT NULL
                GROUP BY 1 ORDER BY 1 DESC LIMIT 7""").fetchall()
        print("  最近的结算批次：")
        for r in rows:
            print(f"    {r['d']}  {r['n']} 行")
        msg = c.execute(
            """SELECT max(received_at AT TIME ZONE 'Asia/Shanghai') last, count(*) n
                 FROM quant.remote_analyst_messages""").fetchone()
        print(f"\n  远端消息：共 {msg['n']} 条，最后接收 {msg['last']}")
        obs = c.execute(
            """SELECT max(created_at AT TIME ZONE 'Asia/Shanghai') last, count(*) n
                 FROM quant.analyst_observations""").fetchone()
        print(f"  抽取观点：共 {obs['n']} 条，最后抽取 {obs['last']}")

    guard("五之二、结算与同步是否真的跑过（区分『没样本』和『没跑』）", freshness)

    section("六、门禁提醒")
    print("  · analyst 策略在 strategy_promotion_registry 中仍为 disabled / max_live_weight=0")
    print("  · 上一次周报（08-10~14）的结论是 0 matured / 1,771 pending，样本门槛未过")
    print("  · 任何 n < 20 的命中率都不足以支持调权（见 strategy_validation 的样本下限）")
