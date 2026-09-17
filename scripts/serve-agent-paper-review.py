"""Local-only, read-only review server for the agent paper accounts (127.0.0.1).

Serves one page plus JSON reads straight from the database, so the page shows
new decisions as soon as a runner writes them.  Nothing here writes to the
ledger, the broker tables or any strategy.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
from typing import Any
from urllib.parse import parse_qs, urlsplit
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "quant-service"))
from dotenv import load_dotenv  # noqa: E402

PAGE = Path(__file__).resolve().parent / "agent-paper-review" / "index.html"
SEARCH_TOOLS = {"WebSearch": "query", "WebFetch": "url"}


def _default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def days(connection: Any) -> list[str]:
    rows = connection.execute(
        """SELECT trading_date FROM quant.agent_paper_decisions
           UNION SELECT trading_date FROM quant.agent_paper_nav ORDER BY 1 DESC""").fetchall()
    return [row["trading_date"].isoformat() for row in rows]


def searches(transcript: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    found = []
    for entry in transcript or []:
        if entry.get("type") == "tool_use" and entry.get("name") in SEARCH_TOOLS:
            found.append({"tool": entry["name"], "target": str((entry.get("input") or {}).get(SEARCH_TOOLS[entry["name"]], ""))})
    return found


def day_view(connection: Any, day: date) -> dict[str, Any]:
    from app.agent_paper.report import status
    accounts = [row["account_key"] for row in connection.execute(
        "SELECT account_key FROM quant.agent_paper_accounts ORDER BY created_at, account_key").fetchall()]
    latest_day = max((date.fromisoformat(d) for d in days(connection)), default=day)
    result = {"day": day.isoformat(), "latest_day": latest_day.isoformat(), "accounts": [], "human": None}
    for key in accounts:
        report = status(connection, account_key=key, day=day, decisions=0)
        daily = next((row for row in report["daily"] if row["trading_date"] == day.isoformat()), None)
        if daily and result["human"] is None:
            result["human"] = {"equity": daily.get("human_equity"), "return_pct": daily.get("human_return_pct"),
                               "basis": daily.get("human_basis"), "comparable": daily.get("human_comparable")}
        navs = [dict(row) for row in connection.execute(
            """SELECT as_of,equity,cash,market_value,price_basis,positions FROM quant.agent_paper_nav
                WHERE account_key=%s AND trading_date=%s ORDER BY as_of""", (key, day)).fetchall()]
        positions = report["positions"] if day == latest_day else (navs[-1]["positions"] if navs else [])
        orders = report["orders"]
        by_decision: dict[str, list[dict[str, Any]]] = {}
        for order in connection.execute(
                """SELECT decision_id,order_id,placed_at,symbol,name,side,order_type,quantity,limit_price,status,filled_quantity,
                          fill_price,fees,reason,reject_reasons,filled_at,closed_at,quote
                     FROM quant.agent_paper_orders WHERE account_key=%s AND trading_date=%s ORDER BY placed_at""",
                (key, day)).fetchall():
            by_decision.setdefault(str(order["decision_id"]), []).append(dict(order))
        decisions = []
        for row in connection.execute(
                """SELECT decision_id,decided_at,status,model,error,duration_ms,context_chars,output,outcomes,usage,
                          context IS NOT NULL AS has_context,transcript
                     FROM quant.agent_paper_decisions WHERE account_key=%s AND trading_date=%s ORDER BY decided_at""",
                (key, day)).fetchall():
            output = row["output"] or {}
            transcript = row["transcript"]
            decisions.append({
                "decision_id": str(row["decision_id"]), "decided_at": row["decided_at"], "status": row["status"],
                "error": row["error"], "duration_ms": row["duration_ms"], "context_chars": row["context_chars"],
                "market_view": output.get("market_view"), "notes": output.get("notes"),
                "focus_symbols": output.get("focus_symbols") or [], "proposed": output.get("orders") or [],
                "outcomes": row["outcomes"], "ledger_orders": by_decision.get(str(row["decision_id"]), []),
                "has_context": row["has_context"], "has_transcript": transcript is not None,
                "tool_calls": sum(1 for e in transcript or [] if e.get("type") == "tool_use" and e.get("name") in SEARCH_TOOLS),
                "searches": searches(transcript), "cost_usd": (row["usage"] or {}).get("total_cost_usd"),
            })
        result["accounts"].append({
            "account_key": key, "model": report["model"], "start_date": report["start_date"],
            "initial_equity": report["initial_equity"], "cash": report["cash"], "daily": daily, "navs": navs,
            "positions": positions, "orders": orders, "decisions": decisions,
        })
    result["human_trades"] = human_view(connection, day)
    return result


def human_view(connection: Any, day: date) -> dict[str, Any]:
    """The owner's broker fills for the day plus their own journal statements, side by side with the agents."""
    fills = [dict(row) for row in connection.execute(
        """SELECT trade_time,symbol,name,side,quantity,price,gross_amount,metadata->>'order_number' AS order_number,
                  metadata->>'order_time' AS order_time
             FROM quant.broker_trade_records
            WHERE account_key='citics-primary' AND trade_date=%s AND trade_time IS NOT NULL
            ORDER BY trade_time,symbol""", (day,)).fetchall()]
    orders: dict[tuple[str, str], dict[str, Any]] = {}
    for fill in fills:
        key = (fill["symbol"], fill["order_number"] or str(fill["trade_time"]))
        order = orders.setdefault(key, {"symbol": fill["symbol"], "name": fill["name"], "side": fill["side"],
                                        "order_number": fill["order_number"], "order_time": fill["order_time"],
                                        "first_fill_time": fill["trade_time"], "quantity": 0, "amount": 0.0, "fills": 0})
        order["quantity"] += int(fill["quantity"])
        gross = fill["gross_amount"] if fill["gross_amount"] is not None else fill["quantity"] * fill["price"]
        order["amount"] += float(gross)
        order["fills"] += 1
    for order in orders.values():
        order["avg_price"] = round(order["amount"] / order["quantity"], 4) if order["quantity"] else None
    journal = [dict(row) for row in connection.execute(
        """SELECT entry_date,title,body,actions,plans,metadata->>'symbol' AS symbol,metadata->>'name' AS name,created_at
             FROM quant.personal_journal_entries
            WHERE source='human_review_session' AND entry_date=%s ORDER BY created_at""", (day,)).fetchall()]
    return {"orders": sorted(orders.values(), key=lambda o: o["first_fill_time"]), "journal": journal}


def decision_detail(connection: Any, decision_id: str, part: str) -> dict[str, Any] | None:
    column = {"context": "context", "transcript": "transcript", "output": "output"}[part]
    row = connection.execute(
        f"SELECT account_key,decided_at,{column} AS body FROM quant.agent_paper_decisions WHERE decision_id=%s",
        (decision_id,)).fetchone()
    return dict(row) if row else None


class Handler(BaseHTTPRequestHandler):
    database: Any
    lock = threading.Lock()

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        if content_type.startswith("text/html"):
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                             "style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, value: Any, status: int = 200) -> None:
        self._send(status, json.dumps(value, ensure_ascii=False, default=_default).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        try:
            if url.path == "/healthz":
                return self._json({"status": "ok", "scope": "local_agent_paper_review"})
            if url.path in {"/", "/index.html"}:
                return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            with self.lock, self.database.transaction() as connection:
                if url.path == "/api/days":
                    return self._json({"days": days(connection)})
                if url.path == "/api/day":
                    raw = (query.get("date") or [""])[0]
                    available = days(connection)
                    selected = date.fromisoformat(raw) if raw else date.fromisoformat(available[0]) if available else date.today()
                    return self._json(day_view(connection, selected))
                if url.path.startswith("/api/decision/"):
                    decision_id = url.path.rsplit("/", 1)[-1]
                    part = (query.get("part") or ["context"])[0]
                    uuid.UUID(decision_id)
                    if part not in {"context", "transcript", "output"}:
                        return self._json({"error": "unknown part"}, 400)
                    detail = decision_detail(connection, decision_id, part)
                    return self._json(detail or {"error": "not found"}, 200 if detail else 404)
            return self._json({"error": "not found"}, 404)
        except ValueError as error:
            return self._json({"error": str(error)}, 400)
        except Exception as error:  # keep the resident server alive; the page shows the error
            return self._json({"error": f"{type(error).__name__}: {error}"}, 500)

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(r"G:\StockPlatform\config\runtime.env"))
    parser.add_argument("--port", type=int, default=15792)
    args = parser.parse_args()
    load_dotenv(args.env_file, override=True)
    from app.database import Database
    Handler.database = Database()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    try:
        server.serve_forever()
    finally:
        Handler.database.close()


if __name__ == "__main__":
    main()
