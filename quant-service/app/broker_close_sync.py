"""The single post-close holdings sync (user authorization of 2026-09-17).

Scope of that authorization, enforced here:

* one run per trading day, 15:00-15:40 Shanghai (``validate_manual_request``);
* the broker window may be switched to 查询 → 资金股份 by clicking the left
  navigation tree, at most ``MAX_CLICKS`` times, nothing else is clicked;
* no scrolling: a list that does not fit on screen fails the run.

A vision model only reads screenshots (Read tool, no shell or writes).  Two
independent reads must agree and reconcile with the displayed totals before an
envelope is built; persistence goes through the audited ``broker-fact-sync``
CLI exactly like a manual run.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
import os
import shutil
import subprocess
from typing import Any

MAX_CLICKS = 2
NAV_TARGETS = {"资金股份", "查询"}

_AMOUNT = {"type": ["string", "null"]}
READ_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["page", "nav", "client_alias", "list_complete", "account", "rows", "notes"],
    "properties": {
        "page": {"type": "string", "enum": ["funds_holdings", "other", "dialog", "unreadable"]},
        "nav": {
            "type": "object", "additionalProperties": False, "required": ["资金股份", "查询"],
            "properties": {name: {"type": ["object", "null"], "additionalProperties": False, "required": ["x", "y"],
                                  "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}}}
                           for name in ("资金股份", "查询")},
        },
        "client_alias": {"type": ["string", "null"]},
        "list_complete": {"type": "boolean"},
        "account": {
            "type": ["object", "null"], "additionalProperties": False,
            "required": ["funds_balance", "available", "withdrawable", "frozen", "stock_market_value", "total_asset",
                         "holding_pnl", "day_pnl", "day_pnl_ratio"],
            "properties": {key: _AMOUNT for key in ("funds_balance", "available", "withdrawable", "frozen",
                                                    "stock_market_value", "total_asset", "holding_pnl", "day_pnl",
                                                    "day_pnl_ratio")},
        },
        "rows": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["code", "name", "balance", "actual_quantity", "sellable", "frozen", "cost", "price",
                         "floating_pnl", "day_pnl"],
            "properties": {"code": {"type": "string"}, "name": {"type": "string"},
                           **{key: _AMOUNT for key in ("balance", "actual_quantity", "sellable", "frozen", "cost",
                                                       "price", "floating_pnl", "day_pnl")}},
        }},
        "notes": {"type": "string"},
    },
}

READ_PROMPT = """你是只读的截图读数员。用 Read 工具打开当前目录下的 {image}（同花顺“网上股票交易系统5.0”整窗截图），只根据图中实际可见的文字作答，不要推测、不要补全。

- page：页面右侧是“资金股份”（上方有资金余额/总资产等汇总，下方是持仓表格）时为 funds_holdings；弹窗遮挡为 dialog；看不清为 unreadable；其他页面为 other。
- nav：左侧导航树里“资金股份”和“查询[F4]”两行文字中心点的像素坐标（以截图左上角为原点）；该行不可见时为 null。
- client_alias：顶部账户下拉框里的文字（例如“中信证券-某*某”），看不到为 null。
- account：仅当 page=funds_holdings 时照抄汇总区数字（去掉千分位逗号，保留原小数位和负号；day_pnl_ratio 保留百分号），否则为 null。
- rows：持仓表格的每一行，按从上到下顺序照抄；“股份余额”填 balance，“实际数量”填 actual_quantity，“可用股份”填 sellable，“冻结数量”填 frozen，“成本价”填 cost，“当前价”填 price，“浮动盈亏”填 floating_pnl，“当日盈亏”填 day_pnl。不含“汇总”行。
- list_complete：表格最后一行之后能看到空白或“汇总”行、且没有被纵向滚动条隐藏的行时为 true。
- notes：简短说明任何看不清或存疑的地方。
"""
SYSTEM_PROMPT = "你只读取截图并按 JSON schema 输出，不执行任何其他操作。"


class CloseSyncError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


def dec(value: Any, label: str) -> Decimal:
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, AttributeError) as error:
        raise CloseSyncError("BROKER_UI_VALUE_UNREADABLE", label) from error


def exchange_symbol(code: str) -> str:
    code = code.strip()
    if len(code) != 6 or not code.isdigit():
        raise CloseSyncError("BROKER_UI_SYMBOL_UNREADABLE", code)
    if code.startswith(("92", "4", "8")):
        return code + ".BJ"
    if code.startswith(("6", "9")):
        return code + ".SH"
    if code.startswith(("0", "2", "3")):
        return code + ".SZ"
    raise CloseSyncError("BROKER_UI_SYMBOL_UNREADABLE", code)


def navigation_pane(windows: list[dict[str, Any]], target: dict[str, Any]) -> dict[str, int]:
    """Window-relative rect of the visible left navigation tree of the target window."""
    left, top = target["rect"]["left"], target["rect"]["top"]
    panes = [w["rect"] for w in windows
             if w.get("pid") == target["pid"] and w.get("visible") and w.get("title") == "HexinScrollWnd2"
             and 0 <= w["rect"]["left"] - left <= 20 and 0 < w["rect"]["width"] <= 260 and w["rect"]["height"] > 200]
    if len(panes) != 1:
        raise CloseSyncError("BROKER_NAV_TREE_NOT_FOUND", f"{len(panes)} candidates")
    pane = panes[0]
    return {"left": pane["left"] - left, "top": pane["top"] - top, "right": pane["right"] - left, "bottom": pane["bottom"] - top}


def click_target(read: dict[str, Any], pane: dict[str, int]) -> tuple[str, int, int]:
    """The only clicks allowed: 资金股份, else 查询 to expand it; both inside the navigation tree."""
    nav = read.get("nav") or {}
    for label in ("资金股份", "查询"):
        point = nav.get(label)
        if point is None:
            continue
        x, y = int(point["x"]), int(point["y"])
        if not (pane["left"] <= x < pane["right"] and pane["top"] <= y < pane["bottom"]):
            raise CloseSyncError("BROKER_NAV_CLICK_OUTSIDE_TREE", f"{label} at {x},{y}")
        if label == "资金股份" and nav.get("查询") and y <= int(nav["查询"]["y"]):
            raise CloseSyncError("BROKER_NAV_CLICK_REJECTED", "资金股份 must be below 查询")
        return label, x, y
    raise CloseSyncError("BROKER_NAV_TARGET_NOT_VISIBLE")


def normalized(read: dict[str, Any]) -> dict[str, Any]:
    """Numeric form of one read, for comparing two independent reads."""
    account = read.get("account") or {}
    def number(value: Any, label: str) -> str | None:
        return None if value in (None, "") else str(dec(str(value).rstrip("%"), label).normalize())
    return {
        "account": {key: number(value, key) for key, value in sorted(account.items())},
        "rows": [{"code": row["code"].strip(), **{key: number(row.get(key), key) for key in
                  ("balance", "actual_quantity", "sellable", "frozen", "cost", "price", "floating_pnl", "day_pnl")}}
                 for row in read.get("rows") or []],
        "list_complete": bool(read.get("list_complete")),
    }


def alias_matches(read_alias: str | None, prior_alias: str | None) -> bool:
    """The client alias is masked (应*药); compare the part before the mask and its length."""
    if not read_alias or not prior_alias or "*" not in prior_alias:
        return False
    head = prior_alias.split("*", 1)[0]
    return read_alias.strip().startswith(head) and len(read_alias.strip()) == len(prior_alias)


@dataclass
class Capture:
    path: str
    sha256: str
    receipt_path: str
    receipt: dict[str, Any]


def build_envelope(*, run: dict[str, Any], reads: list[dict[str, Any]], capture: Capture, prior_identity: dict[str, Any],
                   trade_date: str, clicks: list[dict[str, Any]]) -> dict[str, Any]:
    first = reads[0]
    if first.get("page") != "funds_holdings" or not first.get("account"):
        raise CloseSyncError("BROKER_UI_NOT_ON_FUNDS_PAGE")
    views = [normalized(read) for read in reads]
    if any(view != views[0] for view in views[1:]):
        raise CloseSyncError("BROKER_UI_READS_DISAGREE", json.dumps(views, ensure_ascii=False)[:600])
    if not all(read.get("list_complete") for read in reads):
        raise CloseSyncError("BROKER_UI_LIST_INCOMPLETE", "holdings list does not fit on screen; scrolling is not allowed")
    if not alias_matches(first.get("client_alias"), prior_identity.get("current_client_alias")):
        raise CloseSyncError("BROKER_ACCOUNT_IDENTITY_MISMATCH", f"client alias {first.get('client_alias')!r}")
    account = first["account"]
    displayed_value = dec(account["stock_market_value"], "stock_market_value")
    positions, open_values, day_pnl, zero_rows = [], Decimal(0), Decimal(0), 0
    for row in first["rows"]:
        actual = dec(row["actual_quantity"], "actual_quantity")
        day_pnl += dec(row.get("day_pnl") or 0, "day_pnl")
        if actual == 0:
            zero_rows += 1
            continue
        price, cost = dec(row["price"], "price"), dec(row["cost"], "cost")
        value = actual * price
        open_values += value
        metadata = {"displayed_balance": row["balance"], "displayed_actual_quantity": row["actual_quantity"],
                    "displayed_sellable_quantity": row["sellable"], "displayed_frozen_quantity": row["frozen"],
                    "day_pnl": row["day_pnl"], "market_value_reconciled_from_displayed_quantity_and_price": True,
                    "source_page": 1, "visually_verified": True}
        if cost < 0:
            metadata.update(displayed_average_cost=row["cost"],
                            average_cost_main_field_null_reason="schema_rejects_legitimate_negative_broker_cost")
        positions.append({"symbol": exchange_symbol(row["code"]), "name": row["name"].strip(),
                          "quantity": str(actual), "sellable_quantity": str(dec(row["sellable"], "sellable")),
                          "average_cost": None if cost < 0 else str(cost), "market_price": str(price),
                          "market_value": str(value), "unrealized_pnl": str(dec(row["floating_pnl"], "floating_pnl")),
                          "metadata": metadata})
    if open_values != displayed_value:
        raise CloseSyncError("BROKER_UI_MARKET_VALUE_MISMATCH", f"rows {open_values} != displayed {displayed_value}")
    displayed_day_pnl = dec(account["day_pnl"], "day_pnl")
    if abs(day_pnl - displayed_day_pnl) > Decimal("0.02"):
        raise CloseSyncError("BROKER_UI_DAY_PNL_MISMATCH", f"rows {day_pnl} != displayed {displayed_day_pnl}")
    if not positions and not first["rows"] and displayed_value != 0:
        raise CloseSyncError("BROKER_UI_EMPTY_UNPROVEN")
    receipt = capture.receipt
    identity = {**prior_identity, "broker_basis": "沿用先前用户确认的账户绑定；收盘定时截图的客户端别名与已入库绑定别名一致"}
    return {
        "schema_version": "ths-desktop-holdings-v1", "trigger": "scheduled_close", "source": "ths_desktop_ui",
        "run_id": str(run["run_id"]), "account_key": run["account_key"], "observed_at": receipt["observed_at"],
        "trade_date": trade_date, "account_identity": identity, "account_binding": run["account_binding"],
        "extraction": {
            "method": "window_capture_verified", "capture_method": "win32_hwnd",
            "review": f"收盘定时任务：同一截图由 {len(reads)} 次独立读数得到一致结果；开放持仓按实际数量乘当前价合计等于证券市值，"
                      f"逐行当日盈亏合计等于汇总；导航点击 {len(clicks)} 次（仅限资金股份/查询），未滚动。",
            "readers": len(reads), "navigation_clicks": clicks,
            "account_reconciliation": {
                "displayed_funds_balance": account["funds_balance"], "displayed_available_cash": account["available"],
                "displayed_withdrawable_cash": account["withdrawable"], "displayed_frozen_cash": account["frozen"],
                "displayed_stock_market_value": account["stock_market_value"],
                "displayed_total_assets": account["total_asset"], "displayed_holding_pnl": account["holding_pnl"],
                "displayed_day_pnl": account["day_pnl"], "displayed_day_pnl_ratio": account["day_pnl_ratio"],
                "open_position_market_value_sum": str(open_values), "row_day_pnl_sum": str(day_pnl),
                "total_asset_identity_forced": False,
            },
        },
        "completeness": {
            "all_positions_visible": True, "position_count": len(positions), "visible_row_count": len(first["rows"]),
            "holdings_pages": [1], "end_of_list_verified": True, "account_totals_verified": True,
            "explicit_empty": not positions, "horizontal_fields_complete": False, "required_fields_reconciled": True,
            "zero_actual_rows_excluded": zero_rows,
        },
        "evidence": [{
            "path": capture.path.replace("\\", "/"), "sha256": capture.sha256.lower(), "captured_at": receipt["observed_at"],
            "kind": "screenshot", "page": 1, "roles": ["account_identity", "account_totals", "holdings"],
            "capture_receipt": capture.receipt_path.replace("\\", "/"), "target_pid": receipt.get("targetPID"),
            "target_hwnd": receipt.get("targetHWND"), "target_path": str(receipt.get("targetPath", "")).replace("\\", "/"),
            "window_title": receipt.get("title"), "foreground_before": receipt.get("foregroundBefore"),
            "foreground_after": receipt.get("foregroundAfter"),
        }],
        "account": {"cash": str(dec(account["available"], "available")),
                    "total_asset": str(dec(account["total_asset"], "total_asset")),
                    "total_market_value": str(displayed_value)},
        "positions": positions,
    }


class ScreenshotReader:
    """Claude Code CLI with the Read tool only, run inside the evidence directory."""

    def __init__(self, *, model: str | None = None, timeout_seconds: int = 180) -> None:
        self.model = model or os.environ.get("BROKER_CLOSE_SYNC_MODEL") or "claude-opus-5"
        self.binary = os.environ.get("AGENT_PAPER_CLAUDE_BIN") or shutil.which("claude") or "claude"
        self.proxy = os.environ.get("AGENT_PAPER_CLI_PROXY") or ""
        self.timeout_seconds = timeout_seconds

    def command(self, image: str) -> list[str]:
        return [self.binary, "-p", "--model", self.model, "--output-format", "json", "--tools", "Read",
                "--allowedTools", "Read", "--no-session-persistence", "--strict-mcp-config", "--disable-slash-commands",
                "--system-prompt", SYSTEM_PROMPT, "--json-schema", json.dumps(READ_SCHEMA, ensure_ascii=False),
                READ_PROMPT.format(image=image)]

    def read(self, directory: str, image: str) -> dict[str, Any]:
        from .agent_paper.model import ModelFailure, parse_cli_result
        env = dict(os.environ)
        if self.proxy:
            for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                env[name] = self.proxy
            env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost"
        try:
            completed = subprocess.run(self.command(image), capture_output=True, text=True, encoding="utf-8", cwd=directory,
                                       env=env, timeout=self.timeout_seconds, stdin=subprocess.DEVNULL,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if not completed.stdout.strip():
                raise CloseSyncError("BROKER_UI_READER_FAILED", f"exit {completed.returncode}: {completed.stderr[-300:]}")
            output, _ = parse_cli_result(completed.stdout)
        except subprocess.TimeoutExpired as error:
            raise CloseSyncError("BROKER_UI_READER_FAILED", "timeout") from error
        except ModelFailure as error:
            raise CloseSyncError("BROKER_UI_READER_FAILED", f"{error.code}: {error.detail}"[:300]) from error
        return output


__all__ = ["Capture", "CloseSyncError", "MAX_CLICKS", "READ_SCHEMA", "ScreenshotReader", "alias_matches",
           "build_envelope", "click_target", "exchange_symbol", "navigation_pane", "normalized"]
