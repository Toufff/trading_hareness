"""Local exact-code persistence for THS concept limit-up candidates.

This module owns no provider request, scheduler or strategy score.  It only
joins already-persisted concept membership to an already-fetched limit pool,
so callers can keep the database portion inside the bounded DB executor.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Callable

from .sector_membership_repository import point_in_time_membership_predicate


def select_concepts(database: Any, requested_date: date | None, top_concepts: int) -> tuple[date | None, list[Any]]:
    """Select bounded positive-flow concepts from locally persisted evidence."""
    with database.transaction() as connection:
        latest = connection.execute(
            "SELECT max(trading_date) latest FROM quant.sector_market_observations WHERE taxonomy_key='ths_concept_flow'"
        ).fetchone()
        selected_date = requested_date or (latest or {}).get("latest")
        if selected_date is None:
            return None, []
        concepts = connection.execute(
            """SELECT o.sector_key,s.label,o.net_amount
                 FROM quant.sector_market_observations o
                 JOIN quant.sectors s ON s.taxonomy_key=o.taxonomy_key AND s.sector_key=o.sector_key
                WHERE o.taxonomy_key='ths_concept_flow' AND o.trading_date=%s AND o.net_amount IS NOT NULL
                ORDER BY o.net_amount DESC,s.label LIMIT %s""",
            (selected_date, top_concepts),
        ).fetchall()
    return selected_date, concepts


def persist_members(
    database: Any,
    sector_key: str,
    rows: list[dict[str, Any]],
    provider: str,
    observed_at: datetime,
    persist_member_rows: Callable[..., int],
) -> int:
    """Persist one exact ``ths_member`` snapshot through the existing writer."""
    with database.transaction() as connection:
        return persist_member_rows(connection, "ths_concept_flow", sector_key, rows, provider, observed_at)


def persist_candidates(
    database: Any,
    selected_date: date,
    concepts: list[Any],
    concept_keys: list[str],
    limit_provider: str,
    limit_by_symbol: dict[str, dict[str, Any]],
    membership_status: dict[str, str],
    observed_at: datetime,
    leaders_per_concept: int,
    number: Callable[[Any], float | None],
    decimal_value: Callable[[Any], Any],
    json_value: Callable[[Any], Any],
) -> tuple[int, list[dict[str, Any]]]:
    """Write top limit-up members for each exact, same-date concept relation."""
    membership_predicate = point_in_time_membership_predicate("member")
    with database.transaction() as connection:
        memberships = connection.execute(
            f"""SELECT sector_key,symbol,raw FROM quant.sector_membership_history member
                 WHERE taxonomy_key='ths_concept_flow' AND sector_key = ANY(%s) AND {membership_predicate}""",
            (concept_keys, selected_date, selected_date, selected_date),
        ).fetchall()
        members_by_sector: dict[str, list[dict[str, Any]]] = {}
        for row in memberships:
            item = dict(row)
            members_by_sector.setdefault(str(item["sector_key"]), []).append(item)
        connection.execute(
            """DELETE FROM quant.sector_limit_candidates
                 WHERE taxonomy_key='ths_concept_flow' AND trading_date=%s AND provider_key=%s AND sector_key = ANY(%s)""",
            (selected_date, limit_provider, concept_keys),
        )
        stored = 0
        per_concept: list[dict[str, Any]] = []
        for concept in concepts:
            sector_key = str(concept["sector_key"])
            matches = [
                (member, limit_by_symbol[str(member["symbol"]).upper()])
                for member in members_by_sector.get(sector_key, [])
                if str(member["symbol"]).upper() in limit_by_symbol
            ]
            matches.sort(
                key=lambda item: (number(item[1].get("limit_amount")) or 0.0, number(item[1].get("pct_chg")) or 0.0),
                reverse=True,
            )
            selected = matches[:leaders_per_concept]
            for member, row in selected:
                symbol = str(member["symbol"]).upper()
                connection.execute(
                    """INSERT INTO quant.sector_limit_candidates(taxonomy_key,sector_key,symbol,trading_date,provider_key,available_at,
                             name,limit_tag,limit_type,pct_change,price,limit_amount,turnover_rate,open_num,status,description,raw)
                       VALUES('ths_concept_flow',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT(taxonomy_key,sector_key,symbol,trading_date,provider_key) DO UPDATE SET available_at=EXCLUDED.available_at,
                         name=EXCLUDED.name,limit_tag=EXCLUDED.limit_tag,limit_type=EXCLUDED.limit_type,pct_change=EXCLUDED.pct_change,
                         price=EXCLUDED.price,limit_amount=EXCLUDED.limit_amount,turnover_rate=EXCLUDED.turnover_rate,open_num=EXCLUDED.open_num,
                         status=EXCLUDED.status,description=EXCLUDED.description,raw=EXCLUDED.raw""",
                    (
                        sector_key, symbol, selected_date, limit_provider, observed_at, row.get("name"), row.get("tag"),
                        row.get("limit_type"), decimal_value(row.get("pct_chg")), decimal_value(row.get("price")),
                        decimal_value(row.get("limit_amount")), decimal_value(row.get("turnover_rate")),
                        decimal_value(row.get("open_num")), row.get("status"), row.get("lu_desc"),
                        json_value({"limit_list_ths": row, "ths_member": member["raw"],
                                    "membership_fetch_status": membership_status.get(sector_key, "unknown")}),
                    ),
                )
                stored += 1
            per_concept.append({"sector_key": sector_key, "label": concept["label"], "net_amount": concept["net_amount"],
                                "matched_limit_ups": len(matches), "stored": len(selected)})
    return stored, per_concept


__all__ = ["persist_candidates", "persist_members", "select_concepts"]
