"""Reconstruct histories once per cohort, without changing stored evidence."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from .repository import SHANGHAI, PHASES, load, payload, phase_for, timestamp
from .rules import metrics, number, persistence, relative_return, score

SCHEMA_VERSION = 1
MODEL_VERSION = "sector_heat_pg_v1"


def _breadth_index(rows):
    indexed = defaultdict(list)
    for row in rows:
        features = payload(row["features_json"])
        breadth, when = number(features.get("advancing_breadth")), timestamp(row["data_as_of"])
        if breadth is None or not 0 <= breadth <= 1 or when is None:
            continue
        indexed[(row["trade_date"], row["phase"], row["scope_key"])].append(
            (when, breadth, int(row["sample_size"]), row["data_as_of"]))
    for values in indexed.values():
        values.sort(reverse=True)
    return indexed


def _cohorts(records, breadth_rows):
    attempts = defaultdict(list)
    breadth = _breadth_index(breadth_rows)
    for row in records:
        if row["phase"] is None:
            continue
        row["metrics"] = metrics(row["body"])
        group = (row["trade_date"], row["phase"], row["kind"])
        attempts[(*group, row["key"])].append(row)
    cohorts = defaultdict(dict)
    for (day, phase, kind, key), versions in attempts.items():
        versions.sort(key=lambda r: (r.get('series_basis')=='dated_member_aggregate', r["when"], r["received"], r["id"]), reverse=True)
        latest = versions[0]
        chosen = next((r for r in versions if r["quality"] == "ok"
                       and r["metrics"]["return_pct"] is not None
                       and r["metrics"]["flow_ratio"] is not None
                       and r["metrics"]["volume_ratio"] is not None
                       and (latest["when"] - r["when"]).total_seconds() <= 900), latest)
        row = dict(chosen)
        row["metrics"] = dict(row["metrics"])
        row["selection"] = {
            "latest_attempt_at": latest["received_at"], "latest_attempt_quality": latest["quality"],
            "used_earlier_usable": latest["id"] != row["id"],
            "reason": "较晚采集残缺，保留同阶段较早可用事实" if latest["id"] != row["id"] else "本阶段最近观测",
            "source_observation_id": row["id"],
        }
        sample = next((v for v in breadth.get((day, phase, f'{kind}:{row["name"]}'), [])
                       if 0 <= (row["when"] - v[0]).total_seconds() <= 900), None)
        if sample:
            row["metrics"].update(advancing_breadth=sample[1], member_count=sample[2])
            row["selection"].update(breadth_as_of=sample[3], breadth_scope="sample_only")
        cohorts[(day, phase, kind)][key] = row
    for cohort in cohorts.values():
        latest_time = max(r["when"] for r in cohort.values())
        for row in cohort.values():
            row["usable"] = row["quality"] == "ok" and (row['phase']=='close' or (latest_time - row["when"]).total_seconds() <= 900)
    return cohorts


def _rank_and_score(cohorts, axis):
    relatives = {}
    for group, rows in cohorts.items():
        peers = [r["metrics"] for r in rows.values() if r["usable"]]
        for key, row in rows.items():
            relatives[(*group, key)] = relative_return(row["metrics"], peers) if row["usable"] else None
    dates = {day: index for index, day in enumerate(axis)}
    for (day, phase, kind), rows in cohorts.items():
        peers = [r["metrics"] for r in rows.values() if r["usable"]]
        for key, row in rows.items():
            index = dates[day]
            previous_dates = axis[max(0, index - 2):index + 1]
            persist = persistence([relatives.get((d, phase, kind, key))
                if cohorts.get((d,phase,kind),{}).get(key,{}).get('series_basis')==row.get('series_basis') else None
                for d in previous_dates])
            measured = row["metrics"] if row["usable"] else metrics({})
            row["score"] = score(measured, peers, persist if row["usable"] else None)
            row["rank"] = None
        ranked = sorted((r for r in rows.values() if r["score"]["attention"] is not None),
                        key=lambda r: (-r["score"]["attention"], r["key"]))
        for rank, row in enumerate(ranked, 1):
            row["rank"] = rank
        for row in rows.values():
            row["selection"].update(cohort_size=len(rows), ranked_cohort_size=len(ranked))


def _point(day, phase, row=None):
    result = {"trade_date": day, "phase": phase, "data_as_of": None, "rank": None,
              "coverage": 0, "attention_score": None, "activity_score": None,
              "strength_score": None, "risk_score": None}
    if row:
        s = row["score"]
        result.update(data_as_of=row["available_at"], rank=row["rank"], coverage=s["coverage"],
                      attention_score=s["attention"], activity_score=s["activity"],
                      strength_score=s["strength"], risk_score=s["risk"])
    return result


def _comparison(row, previous, current_cohort, previous_cohort):
    if previous and row.get('series_basis') != previous.get('series_basis'):
        return None,None,'数据口径变化，不跨口径比较升温降温'
    if not previous or row["rank"] is None or previous["rank"] is None:
        return None, None, "相邻交易日缺少同阶段可比记录"
    if set(current_cohort) != set(previous_cohort):
        return None, None, "前后板块样本范围变化，排名不直接相减"
    mask = lambda r: {f["key"] for f in r["score"]["factors"] if f["available"]}
    if mask(row) != mask(previous):
        return None, None, "前后评分维度覆盖不同，暂不标升温降温"
    return previous["rank"] - row["rank"], row["score"]["attention"] - previous["score"]["attention"], None


def _state(s, m, change):
    if s["attention"] is None:
        return "证据不足"
    if m["return_pct"] is not None and m["return_pct"] < 0 and (s["activity"] or 0) >= 60:
        return "放量走弱"
    if s["risk"] is not None and s["risk"] >= 60:
        return "分歧偏高"
    if change is not None and change >= 8:
        return "升温"
    if change is not None and change <= -8:
        return "降温"
    return "活跃" if s["activity"] is not None and s["activity"] >= 60 else "平稳"


def build_context(conn, trade_date=None, phase=None, as_of=None, history_days=20):
    as_of = as_of or datetime.now(SHANGHAI).isoformat(timespec="seconds")
    day, cutoff, axis, records, breadth_rows = load(conn, trade_date, as_of, history_days, phase)
    active_phase = phase or phase_for(cutoff.isoformat())
    if active_phase in {"intraday", "manual"}:
        active_phase = phase_for(cutoff.isoformat())
    base = {"schema_version": SCHEMA_VERSION, "model_version": MODEL_VERSION,
            "generated_at": cutoff.isoformat(), "trade_date": day, "phase": active_phase,
            "status": "unavailable", "source": "Longhu 行业/概念已存快照（金额：元）",
            "warnings": [], "summary": {}, "items": [], "history_mode": "reconstructed", "trade_permission": False}
    cohorts = _cohorts(records, breadth_rows)
    _rank_and_score(cohorts, axis)
    visible_days = axis[-max(1, min(20, int(history_days))):]
    previous_day = axis[axis.index(day) - 1] if day in axis and axis.index(day) > 0 else None
    phase_names = [name for name, start in PHASES if day < cutoff.date().isoformat()
                   or start <= cutoff.hour * 100 + cutoff.minute]
    for kind in ("industry", "concept"):
        current = cohorts.get((day, active_phase, kind), {})
        prior = cohorts.get((previous_day, active_phase, kind), {})
        for key, row in current.items():
            s, m = row["score"], row["metrics"]
            stale = active_phase != "close" and (cutoff - row["when"]).total_seconds() > 900
            rank_change, delta, comparison_reason = _comparison(row, prior.get(key), current, prior)
            reasons = []
            if s["relative"] is not None:
                direction = "高" if s["relative"] >= 0 else "低"
                reasons.append(f'较同阶段同类板块中位数{direction}{abs(s["relative"]):.2f}个百分点')
            if m["volume_ratio"] is not None:
                reasons.append(f'量比{m["volume_ratio"]:.2f}倍')
            if m["flow_ratio"] is not None:
                reasons.append(f'大单口径净额占成交额{m["flow_ratio"] * 100:+.2f}%')
            if comparison_reason:
                reasons.append(comparison_reason)
            if stale:
                reasons.append("本阶段事实超过15分钟，只保留历史数字")
            if row["selection"].get("breadth_scope"):
                reasons.append(f'上涨广度来自{m["member_count"]}只样本，不代表全部成分股')
            missing = [f["key"] for f in s["factors"] if not f["available"]]
            daily = [_point(d, active_phase, historical if historical.get('series_basis')==row.get('series_basis') else None)
                     for d in visible_days for historical in [cohorts.get((d, active_phase, kind), {}).get(key,{})]]
            intraday = [_point(day, p, cohorts.get((day, p, kind), {}).get(key)) for p in phase_names]
            base["items"].append({
                "key": key, "name": row["name"], "kind": kind, "code": row["code"],
                "trade_date": day, "data_as_of": row["available_at"], "received_at": row["received_at"],
                "quality": "degraded" if stale or not row["usable"] or missing else "ok",
                "coverage": s["coverage"], "attention_score": None if stale else s["attention"],
                "activity_score": s["activity"], "strength_score": s["strength"], "risk_score": s["risk"],
                "rank": None if stale else row["rank"], "rank_change": None if stale else rank_change,
                "state_label": "历史样本已过期" if stale else _state(s, m, delta), "reasons": reasons,
                "missing": missing, "metrics": m, "factors": s["factors"],
                "metric_basis":row.get('series_basis'),
                "daily_history": daily, "intraday_history": intraday, "selection": row["selection"],
            })
    base["items"].sort(key=lambda r: (r["kind"], r["attention_score"] is None, -(r["attention_score"] or 0), r["key"]))
    items = base["items"]
    missing_dates = [d for d in visible_days if not any((d, active_phase, k) in cohorts for k in ("industry", "concept"))]
    base["summary"] = {"industry_count": sum(i["kind"] == "industry" for i in items),
                       "concept_count": sum(i["kind"] == "concept" for i in items),
                       "scored_count": sum(i["attention_score"] is not None for i in items),
                       "history_dates": visible_days, "missing_dates": missing_dates,
                       "observed_history_dates": len(visible_days) - len(missing_dates),
                       "scope": "同源已采集板块范围，非全市场完整目录"}
    if not items:
        base["warnings"].append("指定交易日/阶段没有可用板块观测，不以其他日期或阶段补齐")
        return base
    base["status"] = "degraded" if any(i["quality"] != "ok" for i in items) else "ready"
    if any("breadth" in i["missing"] for i in items):
        base["warnings"].append("部分板块缺少上涨广度，关注分按实际可用维度计算，覆盖率已列出")
    if missing_dates:
        base["warnings"].append(f"历史窗口有{len(missing_dates)}个交易日缺少同阶段快照，曲线留空且不连算持续性")
    if any(i["selection"]["used_earlier_usable"] for i in items):
        base["warnings"].append("存在较晚残缺重试，详情保留采用事实与最后尝试的两个时间")
    if any(i["state_label"] == "历史样本已过期" for i in items):
        base["warnings"].append("盘中事实已过时，当前关注分和排名已撤下；可查看历史轨迹")
    return base
