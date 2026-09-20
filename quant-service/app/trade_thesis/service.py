"""Application service for capture/shadow evaluation; never changes ranking or orders."""

from __future__ import annotations

from typing import Any, Callable

from .repository import active_thesis, capture, latest_previous_evaluation, persist_evaluation


def capture_evaluate(database: Any, thesis: dict[str, Any], evidence: list[dict[str, Any]], cutoff: str,
                     source_run_id: str, namespace: str = "shadow", *,
                     evaluator: Callable[..., dict[str, Any]] | None = None,
                     previous: dict[str, Any] | None = None) -> dict[str, Any]:
    """Internal trusted entry used by scan adapters; HTTP only supplies a stored source_run_id."""
    if namespace not in {"capture", "shadow", "advisory"}:
        raise ValueError("decision_binding is disabled")
    if evaluator is None:
        from .rules import evaluate_thesis
        evaluator = evaluate_thesis
    with database.transaction() as connection:
        frozen = active_thesis(connection, str(thesis["thesis_id"]), as_of=cutoff)
        if frozen is None:
            frozen = capture(connection, thesis)["thesis"]
            if frozen is None:
                raise RuntimeError("captured thesis could not be read back")
        if previous is None:
            previous = latest_previous_evaluation(connection, str(frozen["thesis_id"]), cutoff, namespace)
        evaluation_input = {**frozen, "source_run_id": source_run_id}
        evaluation = evaluator(evaluation_input, evidence, cutoff, previous)
        return persist_evaluation(connection, evaluation, namespace=namespace)


evaluate_stored = capture_evaluate


__all__ = ["capture_evaluate", "evaluate_stored"]
