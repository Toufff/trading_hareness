"""Tushare raw-to-control-plane normalization transaction."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable
import re

from psycopg.types.json import Json

from .daily_bar_batch_repository import upsert_daily_bars
from .daily_bar_repository import in_instrument_lock_order
from .instrument_registry import INSTRUMENT_CHUNK_SIZE


#: One statement per ``stock_basic`` chunk, in the shared ascending symbol
#: order, in place of one ``ON CONFLICT DO UPDATE`` per symbol.
#:
#: ``stock_basic`` is the *strongest* writer of ``quant.instruments`` on the
#: production Longhu post-close path: ``longhu_market_repository
#: .persist_full_market_close`` calls ``persist_rows(..., 'stock_basic', ...)``
#: before ``persist_rows(..., 'daily', ...)`` inside one caller-owned
#: transaction, so this statement takes its locks first and
#: ``upsert_daily_bars`` merely re-locks rows it already holds.  ``DO UPDATE``
#: row-locks every EXISTING conflicting row (the whole cross-section on any
#: day after the first), so this order is the one the transaction is actually
#: judged by -- sorting the later array while leaving ~5,500 sequential
#: statements in payload order would have fixed nothing.
STOCK_BASIC_INSTRUMENTS_SQL = (
    "INSERT INTO quant.instruments(symbol,exchange,name,industry,list_date,delist_date,is_st,source) "
    "SELECT t.symbol,t.exchange,t.name,t.industry,t.list_date,t.delist_date,t.is_st,%s "
    "FROM unnest(%s::text[],%s::text[],%s::text[],%s::text[],%s::date[],%s::date[],%s::boolean[]) "
    "AS t(symbol,exchange,name,industry,list_date,delist_date,is_st) "
    "ORDER BY 1 "
    "ON CONFLICT(symbol) DO UPDATE SET exchange=EXCLUDED.exchange,"
    "name=coalesce(EXCLUDED.name,quant.instruments.name),"
    "industry=coalesce(EXCLUDED.industry,quant.instruments.industry),"
    "list_date=coalesce(EXCLUDED.list_date,quant.instruments.list_date),"
    "delist_date=coalesce(EXCLUDED.delist_date,quant.instruments.delist_date),"
    "is_st=EXCLUDED.is_st,source=EXCLUDED.source,updated_at=now()"
)


def _text_or_none(value: Any) -> str | None:
    """Keep a text-column value a text-column value.

    The per-row form let psycopg adapt each parameter on its own; an array
    parameter is adapted as a whole, so a payload whose ``name`` is not a
    string would otherwise decide the array's element type for every row.
    ``None`` is preserved because ``coalesce(EXCLUDED.name, ...)`` in the
    conflict clause distinguishes it from the empty string.
    """
    return None if value is None else str(value)


def persist_stock_basic_instruments(
    connection: Any, instruments: dict[str, tuple[Any, ...]], provider_key: str,
    *, chunk_size: int = INSTRUMENT_CHUNK_SIZE,
) -> list[str]:
    """Write one ``stock_basic`` payload's instrument rows, ascending.

    ``instruments`` maps symbol -> (exchange, name, industry, list_date,
    delist_date, is_st); building it by plain assignment while walking the
    payload is what preserves the previous last-duplicate-wins behaviour of
    ~5,500 sequential ``DO UPDATE`` statements.  Returns the symbols written,
    in the order they were written.
    """
    ordered = sorted(instruments)
    size = max(1, int(chunk_size))
    for start in range(0, len(ordered), size):
        chunk = ordered[start:start + size]
        values = [instruments[symbol] for symbol in chunk]
        connection.execute(STOCK_BASIC_INSTRUMENTS_SQL, (
            provider_key, chunk,
            [row[0] for row in values], [row[1] for row in values], [row[2] for row in values],
            [row[3] for row in values], [row[4] for row in values], [row[5] for row in values],
        ))
    return ordered


#: The only ``factor_semantics`` value whose factor may reach a bar table.
#: ``quant.canonical_bars_daily.adj_factor`` is consumed as a cumulative
#: (hfq-style) corporate-action factor: ``close * adj_factor`` must be
#: comparable across dates.  Anything else -- above all a same-day identity
#: placeholder -- is evidence about what a vendor did NOT supply.
CUMULATIVE_FACTOR_SEMANTICS = "corporate_action_cumulative"

#: The only provider family that publishes a corporate-action history for this
#: market.  The semantics check below cannot stand on its own: a vendor that
#: simply OMITS ``factor_semantics`` while sending ``adj_factor='1'`` would be
#: promoted exactly like a real cumulative factor, which is the original defect
#: with one key removed.  Requiring the provider as well means a new vendor is
#: refused by default and has to be added here deliberately.
PROMOTABLE_FACTOR_PROVIDER_PREFIX = "tushare"

#: Absent/empty semantics keep the historical behaviour for a plain tushare
#: cross-section row, which carries no marker at all.
PROMOTABLE_FACTOR_SEMANTICS = ("", CUMULATIVE_FACTOR_SEMANTICS)


def promotable_factor_provider(provider_key: Any) -> bool:
    """Return whether this provider may ever set ``adj_factor`` on a bar."""
    return str(provider_key or "").startswith(PROMOTABLE_FACTOR_PROVIDER_PREFIX)


def promotable_factor_predicate_sql(alias: str = "stage", column: str = "row_data") -> str:
    """Return the SQL twin of :func:`promotable_adjustment_factor`'s semantics test.

    Set-based writers (``annual_daily_backfill``) cannot call the Python
    predicate per row, so they embed this fragment instead of copying the
    literal.  The provider half of the rule is a scalar there and is checked
    with :func:`promotable_factor_provider` before the statement runs.
    """
    values = ",".join(f"'{value}'" for value in PROMOTABLE_FACTOR_SEMANTICS)
    return f"coalesce({alias}.{column}->>'factor_semantics','') IN ({values})"


def promotable_adjustment_factor(row: dict[str, Any], *, provider_key: str) -> bool:
    """Return whether one ``adj_factor`` row may be written onto a daily bar.

    Both halves must hold.  ``provider_key`` must be a tushare route -- the
    only family that publishes corporate-action history -- and the declared
    semantics must be absent (a plain tushare cross-section row) or
    :data:`CUMULATIVE_FACTOR_SEMANTICS`.  A row that fails either half is
    still stored in ``quant.daily_adjustment_factors`` as evidence of what a
    vendor did or did not supply; it simply never becomes a bar field.
    """
    if not promotable_factor_provider(provider_key):
        return False
    semantics = row.get("factor_semantics")
    if semantics in (None, ""):
        return True
    return str(semantics) == CUMULATIVE_FACTOR_SEMANTICS


def normalize_rows(
    connection: Any, api_name: str, rows: list[dict[str, Any]], available_at: datetime,
    *,
    core_apis: set[str] | frozenset[str],
    date_parser: Callable[[Any], Any], exchange_for: Callable[[str], str],
    is_st_security_name: Callable[[Any], bool], ensure_instruments: Callable[[Any, list[str]], None],
    upsert_bar: Callable[[Any, Any], None], daily_bar_type: Callable[..., Any],
    decimal_or_none: Callable[[Any], Any], safe_error_detail: Callable[[str, int], str],
    provider_key: str = "tushare",
) -> int:
    """Promote a deterministic subset of raw rows; preserve row-level warnings."""
    if api_name not in core_apis:
        return 0
    normalized = 0
    # Register every symbol this payload will reference in one batched
    # statement before the row loop, instead of one INSERT ... ON CONFLICT per
    # row.  The same client-side filter as the loop below is applied here so
    # an unparsable ``ts_code`` still produces only a row-level data-quality
    # warning and no instrument.
    #
    # Four APIs are excluded because another statement in this very
    # transaction already owns their instrument rows, so a pre-pass would be
    # a second ~5,500-element array statement writing rows that are rewritten
    # seconds later:
    #   trade_cal            - carries no symbols at all;
    #   stock_basic          - ``persist_stock_basic_instruments`` below writes
    #       full instrument rows for the whole payload in one sorted
    #       statement, which is a strict superset of what a pre-pass would do;
    #   daily / index_daily  - nothing inside the row loop needs the foreign
    #       key: the bars only accumulate in ``pending_bars`` and
    #       ``quant.data_quality_issues`` has no symbol column.  On the
    #       success path ``upsert_daily_bars`` owns the registration (it
    #       upserts every instrument of the payload before writing bars).  On
    #       a database-level failure the whole transaction is aborted, so
    #       neither bars nor instruments are written and a pre-pass would have
    #       bought nothing.  (The per-row ``upsert_bar`` fallback below does
    #       not change that reasoning either way; see its own comment for what
    #       it can and cannot recover.)
    if api_name not in {"trade_cal", "stock_basic", "daily", "index_daily"}:
        ensure_instruments(connection, [
            symbol for symbol in (str(row.get("ts_code") or "").upper() for row in rows)
            if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol)
        ])
    # ``daily``/``index_daily`` rows are the post-close full-market hot path
    # (~5,500 symbols per call).  Their bars are parsed here as before but the
    # actual persistence is deferred to one batched call after the loop
    # instead of ``upsert_bar`` running its 5-6 statements per row.
    pending_bars: list[Any] = []
    # ``stock_basic`` rows are collected the same way and written after the
    # loop by ``persist_stock_basic_instruments``: one sorted statement per
    # payload instead of ~5,500 per-row ``ON CONFLICT DO UPDATE`` statements
    # in payload order.  Plain assignment keyed by symbol reproduces the
    # previous last-duplicate-wins outcome of those sequential statements.
    stock_basic_instruments: dict[str, tuple[Any, ...]] = {}
    for row in rows:
        try:
            if api_name == "trade_cal":
                calendar_date = date_parser(row.get("cal_date"))
                if not calendar_date:
                    raise ValueError("trade_cal row has no cal_date")
                connection.execute("""INSERT INTO quant.market_trade_calendar(exchange,calendar_date,is_open,pretrade_date,provider,available_at,raw)
                       VALUES(%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT(exchange,calendar_date) DO UPDATE SET is_open=EXCLUDED.is_open,pretrade_date=EXCLUDED.pretrade_date, available_at=EXCLUDED.available_at,raw=EXCLUDED.raw""",
                    (str(row.get("exchange") or "SSE"), calendar_date, str(row.get("is_open")) == "1", date_parser(row.get("pretrade_date")), provider_key, available_at, Json(row)))
            elif api_name == "stock_basic":
                symbol = str(row.get("ts_code") or "").upper()
                if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol):
                    raise ValueError("stock_basic row has invalid ts_code")
                stock_basic_instruments[symbol] = (
                    str(row.get("exchange") or exchange_for(symbol)),
                    _text_or_none(row.get("name")), _text_or_none(row.get("industry")),
                    date_parser(row.get("list_date")), date_parser(row.get("delist_date")),
                    bool(is_st_security_name(row.get("name"))),
                )
            elif api_name == "suspend_d":
                symbol = str(row.get("ts_code") or "").upper()
                suspend_date = date_parser(row.get("trade_date") or row.get("suspend_date"))
                if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol) or not suspend_date:
                    raise ValueError("suspend_d row needs ts_code and trade_date")
                resume_date = date_parser(row.get("resume_date"))
                connection.execute("""INSERT INTO quant.security_suspensions(symbol,suspend_date,resume_date,suspend_reason,provider,available_at,raw) VALUES(%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT(symbol,suspend_date,provider) DO UPDATE SET resume_date=EXCLUDED.resume_date,suspend_reason=EXCLUDED.suspend_reason, available_at=EXCLUDED.available_at,raw=EXCLUDED.raw""",
                    (symbol, suspend_date, resume_date, row.get("suspend_timing") or row.get("suspend_reason"), provider_key, available_at, Json(row)))
                if resume_date:
                    connection.execute("""UPDATE quant.canonical_bars_daily SET is_suspended=true,canonicalized_at=now()
                             WHERE symbol=%s AND trading_date >= %s AND trading_date < %s""",
                        (symbol, suspend_date, resume_date))
                else:
                    # Daily cross-section responses normally describe one
                    # suspended trading day and omit ``resume_date``.  Treating
                    # that omission as an open-ended interval would mark every
                    # later bar as suspended during a historical backfill.
                    connection.execute("""UPDATE quant.canonical_bars_daily SET is_suspended=true,canonicalized_at=now()
                             WHERE symbol=%s AND trading_date=%s""", (symbol, suspend_date))
            else:
                symbol = str(row.get("ts_code") or "").upper()
                trading_date = date_parser(row.get("trade_date"))
                if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol) or not trading_date:
                    raise ValueError(f"{api_name} row needs ts_code and trade_date")
                if api_name in {"daily", "index_daily"}:
                    pending_bars.append(daily_bar_type(symbol=symbol, trading_date=trading_date, open=decimal_or_none(row.get("open")), high=decimal_or_none(row.get("high")), low=decimal_or_none(row.get("low")), close=decimal_or_none(row.get("close")), pre_close=decimal_or_none(row.get("pre_close")), volume=decimal_or_none(row.get("vol")), amount=decimal_or_none(row.get("amount")), source=provider_key, available_at=available_at))
                elif api_name == "adj_factor":
                    adj_factor = decimal_or_none(row.get("adj_factor"))
                    if adj_factor is None:
                        raise ValueError("adj_factor row has no positive adj_factor")
                    connection.execute("""INSERT INTO quant.daily_adjustment_factors(symbol,trading_date,adj_factor,provider,available_at,raw) VALUES(%s,%s,%s,%s,%s,%s)
                           ON CONFLICT(symbol,trading_date,provider) DO UPDATE SET adj_factor=EXCLUDED.adj_factor,available_at=EXCLUDED.available_at,raw=EXCLUDED.raw""", (symbol, trading_date, adj_factor, provider_key, available_at, Json(row)))
                    # The canonical UPDATE below is the only place a factor row
                    # becomes a bar field, and it used to be provider-agnostic:
                    # any vendor placeholder overwrote the bar exactly like a
                    # real cumulative tushare factor.  A row that declares its
                    # own non-cumulative semantics stays evidence only, and so
                    # does any row from a provider that publishes no
                    # corporate-action history -- a vendor that merely OMITS
                    # the marker is refused by the provider half of the rule
                    # rather than promoted like the original defect.
                    if promotable_adjustment_factor(row, provider_key=provider_key):
                        connection.execute("UPDATE quant.canonical_bars_daily SET adj_factor=%s,canonicalized_at=now() WHERE symbol=%s AND trading_date=%s", (adj_factor, symbol, trading_date))
                elif api_name == "daily_basic":
                    connection.execute("""INSERT INTO quant.daily_fundamentals(symbol,trading_date,close,turnover_rate,volume_ratio,pe,pb,total_share,float_share,total_mv,circ_mv,provider,available_at,raw)
                           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT(symbol,trading_date,provider) DO UPDATE SET close=EXCLUDED.close,turnover_rate=EXCLUDED.turnover_rate, volume_ratio=EXCLUDED.volume_ratio,pe=EXCLUDED.pe,pb=EXCLUDED.pb,total_share=EXCLUDED.total_share,float_share=EXCLUDED.float_share, total_mv=EXCLUDED.total_mv,circ_mv=EXCLUDED.circ_mv,available_at=EXCLUDED.available_at,raw=EXCLUDED.raw""", (symbol, trading_date, decimal_or_none(row.get("close")), decimal_or_none(row.get("turnover_rate")), decimal_or_none(row.get("volume_ratio")), decimal_or_none(row.get("pe")), decimal_or_none(row.get("pb")), decimal_or_none(row.get("total_share")), decimal_or_none(row.get("float_share")), decimal_or_none(row.get("total_mv")), decimal_or_none(row.get("circ_mv")), provider_key, available_at, Json(row)))
                elif api_name == "stk_limit":
                    up, down = decimal_or_none(row.get("up_limit")), decimal_or_none(row.get("down_limit"))
                    connection.execute("""INSERT INTO quant.daily_trade_limits(symbol,trading_date,limit_up,limit_down,provider,available_at,raw) VALUES(%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT(symbol,trading_date,provider) DO UPDATE SET limit_up=EXCLUDED.limit_up,limit_down=EXCLUDED.limit_down, available_at=EXCLUDED.available_at,raw=EXCLUDED.raw""", (symbol, trading_date, up, down, provider_key, available_at, Json(row)))
                    connection.execute("UPDATE quant.canonical_bars_daily SET limit_up=%s,limit_down=%s,canonicalized_at=now() WHERE symbol=%s AND trading_date=%s", (up, down, symbol, trading_date))
            normalized += 1
        except Exception as error:
            connection.execute("""INSERT INTO quant.data_quality_issues(capability,severity,code,message,details)
                   VALUES(%s,'warning','tushare_normalization_failed',%s,%s)""", (api_name, safe_error_detail(str(error), 500), Json({"row": row})))
    if stock_basic_instruments:
        # Every row above was already counted once it parsed (the same
        # "parsed -> counted" contract the bars branch uses).  There is no
        # per-row fallback here and deliberately so: the only failures left
        # are server-side, and a server-side failure aborts the transaction,
        # so a retry loop could not execute one statement -- nor could the
        # per-row form this replaces, whose ``data_quality_issues`` warning
        # would itself have failed on the aborted transaction.
        persist_stock_basic_instruments(connection, stock_basic_instruments, provider_key)
    if pending_bars:
        # Each bar already incremented ``normalized`` above once it parsed
        # successfully (matching every other branch's "parsed -> counted"
        # contract); the persistence step below never increments it again on
        # success, only decrements it for a bar that fails even the per-row
        # fallback, so the two paths agree on the final count.
        try:
            upsert_daily_bars(connection, pending_bars)
        except Exception:
            # Degrade to the previous one-statement-set-per-bar path so one
            # bad bar cannot silently drop the rest of a full-market
            # cross-section.  Be precise about the reach of this fallback:
            # ``Database.transaction()`` yields a plain psycopg connection
            # inside one ``connection.transaction()`` with no per-bar
            # savepoint, so once the SERVER has raised (a genuine constraint
            # violation) the transaction is aborted and the first fallback
            # statement fails too -- the loop below then re-raises out of
            # ``normalize_rows`` exactly as the batch did.  What it does
            # recover is a failure raised client-side while
            # ``upsert_daily_bars`` builds its arrays (model_dump, sha256,
            # decimal handling) on one malformed bar, where the connection is
            # still usable and the remaining bars can be written one by one.
            # Widening it to server-side errors would require wrapping each
            # bar in a nested ``connection.transaction()`` savepoint.
            # Ascending (symbol, trading_date): the batch statement this
            # falls back from registers the whole cross-section in one
            # sorted statement, and the degraded path must take the same
            # order rather than the provider's.  Sorting instead of hoisting
            # an ensure_instruments call is what keeps the fallback's stored
            # values identical to the batch path's (name/industry/is_st).
            for bar in in_instrument_lock_order(pending_bars):
                try:
                    upsert_bar(connection, bar)
                except Exception as bar_error:
                    normalized -= 1
                    connection.execute(
                        """INSERT INTO quant.data_quality_issues(capability,severity,code,message,details)
                               VALUES(%s,'warning','tushare_normalization_failed',%s,%s)""",
                        (api_name, safe_error_detail(str(bar_error), 500),
                         Json({"symbol": bar.symbol, "trading_date": str(bar.trading_date)})),
                    )
    return normalized


__all__ = [
    "CUMULATIVE_FACTOR_SEMANTICS", "PROMOTABLE_FACTOR_PROVIDER_PREFIX", "PROMOTABLE_FACTOR_SEMANTICS",
    "STOCK_BASIC_INSTRUMENTS_SQL", "normalize_rows", "persist_stock_basic_instruments",
    "promotable_adjustment_factor", "promotable_factor_predicate_sql", "promotable_factor_provider",
]
