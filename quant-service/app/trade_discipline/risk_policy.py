"""The one number that says how much a single name may cost, and where it came from.

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

One number, two lenses, and they are not a second policy:

``stop-loss budget``
    If the hard stop is hit and the exit fills near it, the loss is at most
    the tolerance.  This sets ``max_shares``.

``extreme-loss cap``
    If the stop does *not* work -- an overnight gap, a limit-down with no
    bid -- the loss is still at most the tolerance at the 99th percentile of
    the stage's two-session adverse move.  This sets ``cap_shares``
    (``exposure_calibration.py``).

``recommended_shares`` is the smaller of the two, so the tolerance holds
whether or not the stop functions.  Dropping the cap would not simplify the
policy, it would abandon half of it: at 5% with no cap, 神奇制药 sizes to 6300
shares -- 53.5% of equity in one name -- and a single limit-down move costs
more than 10%.
"""

from __future__ import annotations

from decimal import Decimal

#: What the user accepts losing on one name, in percent of account equity.
#: Both lenses below are derived from this and nothing else.
PER_NAME_LOSS_TOLERANCE_PCT = Decimal("5.0")

#: No separate account-level limit exists. The user was asked directly on
#: 2026-09-19 ("依然单只5%，不会说总的标准不一样") and declined one; positions are
#: bounded per name only.
ACCOUNT_TOTAL_LIMIT_PCT: Decimal | None = None

SOURCE_USER = "user"
SOURCE_OVERRIDE = "cli_override"

POLICY_VERSION = "discipline-risk-policy-v1"


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
            "单只股票可接受亏损为权益的 5%（用户 2026-09-20 设定）。正常止损与极端情形同一标准，"
            "不另设账户总额限制。止损预算与极端亏损上限都由这一个数推出，取更小者。"
        ),
    }


__all__ = [
    "ACCOUNT_TOTAL_LIMIT_PCT",
    "PER_NAME_LOSS_TOLERANCE_PCT",
    "POLICY_VERSION",
    "SOURCE_OVERRIDE",
    "SOURCE_USER",
    "policy_record",
]
