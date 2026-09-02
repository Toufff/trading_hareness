"""Make this relocated legacy migration package importable for its own tests.

These modules used to live inside ``quant-service/app`` where pytest's normal
invocation (``python -m pytest`` from ``quant-service``) put both the ``app``
package and the test's own directory on ``sys.path``.  Now that they live
outside that package, running ``pytest`` against this directory from any
working directory needs the same two things put on ``sys.path`` explicitly:
this directory itself (for the flat sibling imports between the four
``legacy_stock_brain_*`` modules) and ``quant-service`` (for the handful of
``app.*`` imports those modules still need).
"""

from __future__ import annotations

import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_QUANT_SERVICE_ROOT = _THIS_DIR.parents[2] / "quant-service"

for _path in (_THIS_DIR, _QUANT_SERVICE_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
