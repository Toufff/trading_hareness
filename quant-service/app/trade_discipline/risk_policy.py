"""The executable loss budget and the user's concentration preference.

Until 2026-09-20 there were two, and only one of them had ever been asked
about.  The extreme-loss tolerance -- 5% of equity for a single name -- was
given by the user on 2026-09-19 and wired into the exposure calibration.  The
stop-loss budget was ``1.0``, a default from the module's first day that
nobody had put to the user, hardcoded twice (``templates.py`` and the CLI's
own ``--risk-per-trade-pct`` default, the second of which actually won) and
written into prose in two documents and the skill.

The consequence was worse than the duplication: across all seven cards
generated for 2026-09-21 the 1% budget was the binding constraint on every
single one, so the 5% the user had set governed nothing at all, while the
resulting position sizes were described to them as if it had.

On 2026-09-20 the user resolved it: *"正常也是5%，我现在不需要这两个情况分开来"*
-- one tolerance, 5%, for the ordinary case and the extreme case alike.

The 2026-09-21 review separated two things that had incorrectly been coupled:

``stop-loss budget``
    If the hard stop is hit and the exit fills near it, the loss is at most
    the tolerance.  This sets ``max_shares``.

``tail-risk stress``
    The stage/board 99th-percentile two-session adverse move estimates what a
    concentrated position could lose if the stop cannot execute.  It is an
    advisory disclosure, not an order instruction and not a position cap.

``recommended_shares`` follows the stop-loss budget.  A reduction is created
only when that budget (or a separate price/time/event discipline line) is
breached.  High concentration is permitted for a high-conviction trade; the
tail estimate stays visible so the user can make that choice consciously.
"""

from __future__ import annotations

from decimal import Decimal

#: Executable hard-stop risk budget for one name, in percent of equity.
PER_NAME_LOSS_TOLERANCE_PCT = Decimal("5.0")

#: No separate account-level limit exists. The user was asked directly on
#: 2026-09-19 ("依然单只5%，不会说总的标准不一样") and declined one; positions are
#: bounded per name only.
ACCOUNT_TOTAL_LIMIT_PCT: Decimal | None = None

SOURCE_USER = "user"
SOURCE_OVERRIDE = "cli_override"

POLICY_VERSION = "discipline-risk-policy-v2"
CONCENTRATION_POLICY = "tail_risk_advisory"


def policy_record(applied_pct: Decimal) -> dict[str, object]:
    """Provenance for the value a plan actually used.

    A plan has to be able to say whether the number that sized it is the
    standing policy or a one-off ``--risk-per-trade-pct`` on some operator's
    command line. Before this existed a card could not, and a default nobody
    had chosen read exactly like a decision the user had made.
    """
    matches_policy = Decimal(str(applied_pct)) == PER_NAME_LOSS_TOLERANCE_PCT
    return {
        "policy_version": POLICY_VERSION,
        "per_name_loss_tolerance_pct": float(PER_NAME_LOSS_TOLERANCE_PCT),
        "applied_pct": float(applied_pct),
        "source": SOURCE_USER if matches_policy else SOURCE_OVERRIDE,
        "set_at": "2026-09-20",
        "account_total_limit_pct": ACCOUNT_TOTAL_LIMIT_PCT,
        "statement": (
            "单只股票硬止损风险预算为权益的 5%（用户 2026-09-20 设定），不另设账户总额限制。"
            "用户 2026-09-21 明确允许高确信度重仓；阶段/板块极端跌幅只作压力测试提示，"
            "不因仓位比例本身生成减仓动作。"
        ),
        "concentration_policy": CONCENTRATION_POLICY,
        "concentration_set_at": "2026-09-21",
    }


__all__ = [
    "ACCOUNT_TOTAL_LIMIT_PCT",
    "CONCENTRATION_POLICY",
    "PER_NAME_LOSS_TOLERANCE_PCT",
    "POLICY_VERSION",
    "SOURCE_OVERRIDE",
    "SOURCE_USER",
    "policy_record",
]
