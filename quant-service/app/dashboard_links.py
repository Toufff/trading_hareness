"""Small URL helpers shared by human-facing notification renderers."""

from __future__ import annotations


def market_decision_url(configured_url: str | None) -> str | None:
    """Return the final workbench URL without duplicating its path.

    ``QUANT_DASHBOARD_DECISION_URL`` may be a private, cookie-setting gateway
    URL whose final segment is already ``market-decision``.  Older deployments
    only provide the public dashboard origin, which remains supported.
    """

    value = (configured_url or "").strip().rstrip("/")
    if not value:
        return None
    # The private gateway is itself the login action: it sets the access
    # cookie and redirects to the dashboard's default market-decision view.
    # Appending a route after the opaque token would miss nginx's exact match.
    if "/_access/" in value or value.endswith("/market-decision"):
        return value
    return value + "/market-decision"


__all__ = ["market_decision_url"]
