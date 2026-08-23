"""Shared conservative execution timing for A-share research simulations."""

from __future__ import annotations


def a_share_exit_lag(hold_days: int) -> int:
    """Return signal-to-exit trading-index lag under T+1.

    A signal is formed after day ``t`` closes and enters at ``t+1`` open.
    Holding for one complete trading day therefore cannot exit before ``t+2``
    close. Longer requested holding periods add to that entry-day boundary.
    """
    return max(2, int(hold_days) + 1)


__all__ = ["a_share_exit_lag"]
