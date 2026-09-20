"""Human-readable projection of an already-computed evaluation."""
from __future__ import annotations


def evaluation_summary(evaluation: dict) -> str:
    states = evaluation["states"]
    excluded = evaluation.get("evidence_manifest", {}).get("excluded", [])
    changes = evaluation.get("changes_since_previous", [])
    next_checks = evaluation.get("next_checks", [])
    parts = [f"假设 {states['thesis_state']}；证据 {states['evidence_status']}；新买 {states['entry_state']}。"]
    parts.append(f"截止 {evaluation['cutoff_at']}，修订 {evaluation['thesis_revision']}，纳入 {len(evaluation.get('observations', []))} 条证据。")
    if excluded:
        parts.append("排除：" + "、".join(f"{item.get('evidence_id') or item.get('metric')}({item['reason']})" for item in excluded) + "。")
    if changes:
        parts.append("变化：" + "；".join(f"{c['metric']} {c['old_value']}→{c['new_value']} {c.get('unit') or ''}" for c in changes) + "。")
    if next_checks:
        parts.append("下一验证：" + "、".join(str(item.get("metric") or item["condition_id"]) for item in next_checks) + "。")
    return "".join(parts)
