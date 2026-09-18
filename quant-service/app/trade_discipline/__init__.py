"""Machine-derived trade discipline (research-only).

Turns "trigger / cancel / stop / exit / timeline / sizing" from free chat text
into structured, evaluable and auditable lines.  Nothing in this package
connects to a broker or submits an order; a plan is evidence for a human
decision and is always persisted, including when it fails the quality gate.

Import order inside the package stays flat:
``contracts -> stage -> templates -> quality -> generator -> evaluator -> reconcile``.
"""

from __future__ import annotations

from .contracts import (
    CONTRACT_VERSION,
    Action,
    ComplianceRecord,
    Confirm,
    Derivation,
    DisciplinePlan,
    Evaluation,
    FormulaError,
    Line,
    LineState,
    PositionRef,
    QualityCheck,
    Review,
    Sizing,
    eval_expression,
)
from .evaluator import EVALUATOR_VERSION, EvaluationInputs, evaluate, extra_conditions
from .generator import GENERATOR_VERSION, CalendarInfo, GenerationInputs, generate
from .quality import CHECK_IDS, evaluate_quality, failed_checks, quality_passed
from .reconcile import RECONCILER_VERSION, TradeRecord, reconcile, signals_from
from .report import (
    REPORT_VERSION,
    plan_payload,
    render_markdown,
    report_paths,
    write_report,
)
from .stage import InsufficientBars, classify_stage, daily_metrics, normalize_bars
from .templates import (
    TARGET_EXPOSURE_PCT,
    TEMPLATE_VERSION,
    build_lines,
    build_sizing,
    hard_stop_price,
    trail_stop_price,
)

__all__ = [
    "Action", "CHECK_IDS", "CONTRACT_VERSION", "CalendarInfo", "ComplianceRecord", "Confirm",
    "Derivation", "DisciplinePlan", "EVALUATOR_VERSION", "Evaluation", "EvaluationInputs",
    "FormulaError", "GENERATOR_VERSION", "GenerationInputs", "InsufficientBars", "Line", "LineState",
    "PositionRef", "QualityCheck", "RECONCILER_VERSION", "REPORT_VERSION", "Review", "Sizing",
    "TARGET_EXPOSURE_PCT", "TEMPLATE_VERSION", "TradeRecord", "build_lines", "build_sizing",
    "classify_stage", "daily_metrics", "eval_expression", "evaluate", "evaluate_quality",
    "extra_conditions", "failed_checks", "generate", "hard_stop_price", "normalize_bars",
    "plan_payload", "quality_passed", "reconcile", "render_markdown", "report_paths",
    "signals_from", "trail_stop_price", "write_report",
]
