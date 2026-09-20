"""Local-only review server for the agent paper accounts (127.0.0.1).

Serves one page plus JSON reads straight from the database, so the page shows
new decisions as soon as a runner writes them.  The only write is the owner's
own per-order review note (``POST /api/human-note`` into
``quant.personal_journal_entries``): never the ledger, the broker tables, an
agent decision or any strategy.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import sys
import threading
from typing import Any
from urllib.parse import parse_qs, urlsplit
import uuid
from zoneinfo import ZoneInfo

from psycopg.types.json import Json

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "quant-service"))
from dotenv import load_dotenv  # noqa: E402

PAGE = Path(__file__).resolve().parent / "agent-paper-review" / "index.html"
SEARCH_TOOLS = {"WebSearch": "query", "WebFetch": "url"}
SHANGHAI = ZoneInfo("Asia/Shanghai")
JOURNAL_SOURCE = "human_review_session"


def _default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def days(connection: Any) -> list[str]:
    rows = connection.execute(
        """SELECT trading_date FROM quant.agent_paper_decisions
           UNION SELECT trading_date FROM quant.agent_paper_nav
           UNION SELECT start_date FROM quant.agent_paper_accounts
           ORDER BY 1 DESC""").fetchall()
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
        index = next((i for i, row in enumerate(report["daily"]) if row["trading_date"] == day.isoformat()), None)
        daily = report["daily"][index] if index is not None else None
        # ``*_return_pct`` from the report is cumulative against the comparison start;
        # the previous trading day's close is what makes a same-day number possible.
        previous = report["daily"][index - 1] if index else None
        if daily is not None:
            daily = {**daily, "previous_trading_date": previous["trading_date"] if previous else None,
                     "previous_agent_equity": previous["agent_equity"] if previous else None,
                     "previous_human_equity": previous["human_equity"] if previous else None}
        if daily and result["human"] is None:
            result["human"] = {"equity": daily.get("human_equity"), "total_return_pct": daily.get("human_return_pct"),
                               "previous_equity": daily.get("previous_human_equity"),
                               "basis": daily.get("human_basis"), "comparable": daily.get("human_comparable")}
        navs = [dict(row) for row in connection.execute(
            """SELECT as_of,equity,cash,market_value,price_basis,positions FROM quant.agent_paper_nav
                WHERE account_key=%s AND trading_date=%s ORDER BY as_of""", (key, day)).fetchall()]
        latest_nav_row = connection.execute(
            """SELECT trading_date,as_of,equity,cash,market_value,price_basis,positions
                 FROM quant.agent_paper_nav
                WHERE account_key=%s AND trading_date<=%s
                ORDER BY trading_date DESC,as_of DESC LIMIT 1""", (key, day)).fetchone()
        latest_nav = dict(latest_nav_row) if latest_nav_row else None
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
                "analysis": output.get("analysis"),
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
            "latest_nav": latest_nav, "positions": positions, "orders": orders, "decisions": decisions,
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
        """SELECT entry_date,title,body,actions,plans,metadata->>'symbol' AS symbol,metadata->>'name' AS name,
                  metadata->>'order_number' AS order_number,created_at
             FROM quant.personal_journal_entries
            WHERE source='human_review_session' AND entry_date=%s ORDER BY created_at""", (day,)).fetchall()]
    notes = {row["order_number"]: {"body": row["body"],
                                   "plan": (row["plans"] or [{}])[0].get("plan") if row["plans"] else None,
                                   "saved_at": row["created_at"]}
             for row in journal if row["order_number"]}
    return {"orders": sorted(orders.values(), key=lambda o: o["first_fill_time"]),
            "journal": [row for row in journal if not row["order_number"]], "notes": notes}


TEXT_LIMIT = 4000


def save_note(connection: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Store one owner-written note for one broker order; the owner's words, never a system inference."""
    day = date.fromisoformat(str(payload.get("date") or ""))
    symbol = str(payload.get("symbol") or "").upper()
    order_number = str(payload.get("order_number") or "").strip()
    intent = str(payload.get("intent") or "").strip()[:TEXT_LIMIT]
    plan = str(payload.get("plan") or "").strip()[:TEXT_LIMIT]
    if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol):
        raise ValueError("symbol 无效")
    if not re.fullmatch(r"[0-9A-Za-z_-]{1,40}", order_number):
        raise ValueError("order_number 无效")
    fill = connection.execute(
        """SELECT name,side,quantity,price,trade_time FROM quant.broker_trade_records
            WHERE account_key='citics-primary' AND trade_date=%s AND symbol=%s AND metadata->>'order_number'=%s
            ORDER BY trade_time LIMIT 1""", (day, symbol, order_number)).fetchone()
    if fill is None:
        raise ValueError("这一天没有该委托号的成交记录")
    key = f"citics-primary:{day.isoformat()}:{symbol}:{order_number}"
    if not intent and not plan:
        connection.execute("DELETE FROM quant.personal_journal_entries WHERE source=%s AND source_record_key=%s",
                           (JOURNAL_SOURCE, key))
        return {"status": "deleted", "order_number": order_number}
    actions = [{"order_number": order_number, "side": fill["side"], "quantity": int(fill["quantity"]),
                "price": float(fill["price"]), "fill_time": str(fill["trade_time"])}]
    plans = [{"for": "next_trading_day", "plan": plan}] if plan else []
    metadata = {"account_key": "citics-primary", "symbol": symbol, "name": fill["name"],
                "order_number": order_number, "semantics": "owner_statement_not_system_inference",
                "written_via": "review_page"}
    canonical = json.dumps({"body": intent, "actions": actions, "plans": plans, "metadata": metadata},
                           ensure_ascii=False, sort_keys=True, default=str)
    content_hash = sha256(canonical.encode("utf-8")).hexdigest()
    connection.execute("DELETE FROM quant.personal_journal_entries WHERE source=%s AND source_record_key=%s",
                       (JOURNAL_SOURCE, key))
    connection.execute(
        """INSERT INTO quant.personal_journal_entries(
               entry_date,entry_type,title,body,actions,plans,source,source_record_key,content_hash,metadata)
           VALUES(%s,'trade',%s,%s,%s,%s,%s,%s,%s,%s)""",
        (day, f"{fill['name']} {symbol} {order_number}", intent, Json(actions), Json(plans),
         JOURNAL_SOURCE, key, content_hash, Json(metadata)))
    return {"status": "saved", "order_number": order_number, "symbol": symbol,
            "saved_at": datetime.now(SHANGHAI).isoformat()}


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

    def do_POST(self) -> None:
        url = urlsplit(self.path)
        try:
            if url.path != "/api/human-note":
                return self._json({"error": "not found"}, 404)
            # Loopback only: this is the single write path in an otherwise read-only server.
            if self.client_address[0] not in {"127.0.0.1", "::1"}:
                return self._json({"error": "local access only"}, 403)
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= 64_000:
                return self._json({"error": "invalid body length"}, 400)
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                return self._json({"error": "body must be an object"}, 400)
            with self.lock, self.database.transaction() as connection:
                return self._json(save_note(connection, payload))
        except (ValueError, KeyError, UnicodeDecodeError) as error:
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
