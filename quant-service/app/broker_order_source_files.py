"""Finalize successfully imported broker exports without losing their bytes."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import shutil
from typing import Any

from .broker_order_export_parser import ParsedOrderExport


def move_imported_source(
    source: Path, parsed: ParsedOrderExport, *, inbox_root: Path, processed_root: Path,
) -> dict[str, Any]:
    source = Path(source).resolve()
    inbox = Path(inbox_root).resolve()
    try:
        source.relative_to(inbox)
    except ValueError as error:
        raise ValueError("BROKER_ORDER_SOURCE_OUTSIDE_INBOX") from error
    if not source.is_file():
        raise ValueError("BROKER_ORDER_SOURCE_MISSING")

    suffix = source.suffix.lower() or ".xls"
    folder = Path(processed_root).resolve() / str(parsed.max_order_date.year) / f"{parsed.max_order_date.month:02d}"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{parsed.max_order_date.isoformat()}_委托流水_{parsed.sha256[:8]}{suffix}"
    disposition = "moved"
    if target.exists():
        if sha256(target.read_bytes()).hexdigest() != parsed.sha256:
            raise ValueError("BROKER_ORDER_PROCESSED_HASH_CONFLICT")
        source.unlink()
        disposition = "duplicate_source_removed"
    else:
        shutil.move(str(source), str(target))
    if not target.is_file() or sha256(target.read_bytes()).hexdigest() != parsed.sha256:
        raise ValueError("BROKER_ORDER_PROCESSED_READBACK_MISMATCH")
    return {
        "source_disposition": disposition,
        "processed_path": str(target),
        "processed_sha256": parsed.sha256,
    }


__all__ = ["move_imported_source"]
