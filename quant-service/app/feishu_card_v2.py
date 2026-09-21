"""Small, reusable builders for Feishu Card JSON 2.0.

The helpers intentionally stay presentation-only.  They do not fetch data,
decide whether an alert is material, or perform delivery.
"""

from __future__ import annotations

from typing import Any, Iterable


def markdown(content: str, *, size: str = "normal", margin: str = "0px") -> dict[str, Any]:
    return {
        "tag": "markdown",
        "content": str(content),
        "text_size": size,
        "margin": margin,
    }


def tag(text: str, *, color: str = "neutral") -> dict[str, Any]:
    return {
        "tag": "text_tag",
        "text": {"tag": "plain_text", "content": str(text)},
        "color": color,
    }


def metric_strip(items: Iterable[tuple[str, str]], *, element_id: str) -> dict[str, Any]:
    columns = []
    for label, value in items:
        columns.append({
            "tag": "column",
            "width": "weighted",
            "weight": 1,
            "vertical_align": "center",
            "horizontal_align": "left",
            "vertical_spacing": "2px",
            "padding": "8px 10px",
            "background_style": "grey",
            "elements": [
                markdown(str(label), size="notation"),
                markdown(f"**{value}**", size="large"),
            ],
        })
    return {
        "tag": "column_set",
        "element_id": element_id,
        "flex_mode": "flow",
        "horizontal_spacing": "small",
        "columns": columns,
    }


def content_panel(content: str, *, element_id: str, color: str = "grey") -> dict[str, Any]:
    return {
        "tag": "column_set",
        "element_id": element_id,
        "flex_mode": "stretch",
        "columns": [{
            "tag": "column",
            "width": "weighted",
            "weight": 1,
            "padding": "10px 12px",
            "background_style": color,
            "elements": [markdown(content)],
        }],
    }


def collapsible_panel(title: str, content: str, *, element_id: str,
                      expanded: bool = False) -> dict[str, Any]:
    return {
        "tag": "collapsible_panel",
        "element_id": element_id,
        "expanded": expanded,
        "direction": "vertical",
        "vertical_spacing": "small",
        "padding": "8px 10px",
        "background_color": "grey",
        "border": {"color": "grey", "corner_radius": "6px"},
        "header": {
            "title": {"tag": "markdown", "content": f"**{title}**"},
            "padding": "4px 0px",
            "icon": {
                "tag": "standard_icon",
                "token": "down-small-ccm_outlined",
                "color": "grey",
                "size": "16px 16px",
            },
            "icon_position": "right",
            "icon_expanded_angle": 180,
        },
        "elements": [markdown(content, size="notation")],
    }


def open_url_button(label: str, url: str, *, element_id: str) -> dict[str, Any]:
    return {
        "tag": "button",
        "element_id": element_id,
        "type": "primary_filled",
        "size": "large",
        "width": "fill",
        "text": {"tag": "plain_text", "content": label},
        "behaviors": [{"type": "open_url", "default_url": url}],
    }


def card(*, title: str, subtitle: str, summary: str, template: str,
         tags: Iterable[dict[str, Any]], elements: list[dict[str, Any]],
         card_url: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema": "2.0",
        "config": {
            "summary": {"content": summary[:200]},
            "enable_forward": False,
            "update_multi": True,
            "width_mode": "fill",
        },
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": title},
            "subtitle": {"tag": "plain_text", "content": subtitle},
            "text_tag_list": list(tags)[:3],
            "padding": "12px 12px 10px 12px",
        },
        "body": {
            "direction": "vertical",
            "padding": "12px",
            "vertical_spacing": "medium",
            "elements": elements,
        },
    }
    if card_url:
        result["card_link"] = {"url": card_url}
    return result


__all__ = [
    "card", "collapsible_panel", "content_panel", "markdown", "metric_strip",
    "open_url_button", "tag",
]
