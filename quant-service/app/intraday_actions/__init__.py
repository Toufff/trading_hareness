"""Research-only intraday manual decision observations; never submits orders."""

from .contracts import VERSION
from .engine import evaluate

__all__ = ["VERSION", "evaluate"]
