"""Descriptive heuristics, never calibrated probabilities or trading permission."""
from __future__ import annotations

import math
from statistics import median

WEIGHTS = {"strength": .25, "breadth": .25, "activity": .20, "flow": .15, "persistence": .15}
LABELS = {"strength": "相对方向", "breadth": "样本上涨扩散", "activity": "成交活跃",
          "flow": "资金方向", "persistence": "三日持续性"}


def number(value):
    if value in (None, "", "-") or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def bounded(value, scale):
    value = number(value)
    return None if value is None else 50 + 50 * math.tanh(value / scale)


def metrics(body):
    amount, net, volume = number(body.get("amount")), number(body.get("main_net")), number(body.get("volume_ratio"))
    breadth, count = number(body.get("advancing_breadth")), number(body.get("member_count"))
    if amount is not None and amount < 0:
        amount = None
    if volume is not None and volume < 0:
        volume = None
    if breadth is not None and not 0 <= breadth <= 1:
        breadth = None
    return {
        "return_pct": number(body.get("pct_chg") if body.get("pct_chg") is not None else body.get("change_pct")),
        "amount": amount, "main_net": net,
        "flow_ratio": net / amount if amount is not None and amount > 0 and net is not None else None,
        "volume_ratio": volume, "speed": number(body.get("speed")),
        "advancing_breadth": breadth, "member_count": count,
    }


def persistence(values, required=3):
    if len(values) < required or any(v is None for v in values[-required:]):
        return None
    return 100 * sum(v > 0 for v in values[-required:]) / required


def relative_return(current, peers):
    values = [p["return_pct"] for p in peers if p.get("return_pct") is not None]
    return None if current.get("return_pct") is None or not values else current["return_pct"] - median(values)


def score(current, peers, persistence_score=None):
    relative = relative_return(current, peers)
    vr, breadth, flow = current["volume_ratio"], current["advancing_breadth"], current["flow_ratio"]
    scores = {"strength": bounded(relative, 3), "breadth": None if breadth is None else breadth * 100,
              "activity": None if vr is None else bounded(vr - 1, .8),
              "flow": bounded(flow, .05), "persistence": persistence_score}
    values = {"strength": relative, "breadth": breadth, "activity": vr, "flow": flow, "persistence": persistence_score}
    descriptions = {
        "strength": "相对本阶段同类已采集板块涨跌幅中位数，非相对大盘超额收益",
        "breadth": "已核实样本上涨占比；不是完整板块成分覆盖",
        "activity": "供应商量比；缺失时不拿不同板块成交额代替",
        "flow": "供应商大单口径净额÷成交额，不代表机构身份",
        "persistence": "最近三个交易日同阶段相对同类中位数为正的比例；缺日不连算",
    }
    coverage = sum(WEIGHTS[key] for key, value in scores.items() if value is not None)
    factors = []
    for key, weight in WEIGHTS.items():
        available = scores[key] is not None
        effective = weight / coverage if available and coverage else 0
        factors.append({"key": key, "label": LABELS[key], "value": values[key], "score": scores[key],
                        "weight": weight, "effective_weight": effective, "contribution": scores[key] * effective if available else 0,
                        "available": available, "description": descriptions[key]})
    attention = sum(f["contribution"] for f in factors) if coverage >= .6 - 1e-9 else None
    risks = []
    change = current["return_pct"]
    if change is not None:
        risks.append(min(100, max(0, -change) * 12))
    if breadth is not None:
        risks.append((1 - breadth) * 100)
    if flow is not None:
        risks.append(min(100, max(0, -flow) * 500 + (20 if flow < 0 and change is not None and change > 0 else 0)))
    return {**scores, "relative": relative, "coverage": coverage, "attention": attention,
            "risk": max(risks) if risks else None, "factors": factors}
