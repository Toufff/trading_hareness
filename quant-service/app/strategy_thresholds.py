"""Single source of truth for intraday-gain and volume thresholds shared
across setups in the live watchlist confirmation rule, the EAC breakout
research rules and the daily-prior shadow confirmation rules.

Extracting these values changes no comparison logic: every call site still
performs the identical comparison against the identical float.  It only
removes the risk that copies silently drift apart when one site is edited
and the others are not.  Do not change a value here without the promotion
review this repository requires before any live threshold change (see
AGENTS.md "Scope").
"""

from __future__ import annotations

VERSION = "strategy-thresholds-v1"

# Shared intraday-gain ceiling: reject a setup that has already run this far
# to avoid chasing a stock close to its price-limit zone. Used across the
# live watchlist confirmation rule, EAC breakout research and every
# daily-prior shadow confirmation rule.
MAX_ENTRY_INTRADAY_GAIN_PCT = 6.5

# Shared intraday-gain floor for the setups that require at least a modest
# same-session move before confirming (live sector-surge, fuyao-breadth-entry
# and standard entry setups, and both EAC breakout research variants).
STANDARD_ENTRY_MIN_INTRADAY_GAIN_PCT = 1.0

# Shared minute-volume floor reused by the live sector-surge, green-reclaim
# research and fuyao-breadth-entry setups.
STANDARD_MINUTE_VOLUME_MULTIPLE_FLOOR = 3.0
