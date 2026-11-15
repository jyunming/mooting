"""Mail merge for customer notices.

Templates are written by staff and contain fields such as {first_name} or
{invoice.total}. Values come from the customer record, which customers can edit
themselves through their profile page. The output is an HTML e-mail body.
"""
from __future__ import annotations

import re
from typing import Mapping

FIELD = re.compile(r"\{([a-z_]+(?:\.[a-z_]+)*)\}")


class MissingField(KeyError):
    pass


def escape_html(text: str) -> str:
    """Make text safe to place between HTML tags or inside a quoted attribute."""
    for raw, safe in (("<", "&lt;"), (">", "&gt;"), ('"', "&quot;"), ("'", "&#x27;"), ("&", "&amp;")):
        text = text.replace(raw, safe)
    return text


def lookup(record: Mapping, dotted: str) -> str:
    """record['invoice']['total'] for 'invoice.total'."""
    node = record
    for part in dotted.split("."):
        if not isinstance(node, Mapping) or part not in node:
            raise MissingField(dotted)
        node = node[part]
    return str(node)


def fields(template: str) -> list[str]:
    """The distinct fields a template uses, in order of first appearance."""
    seen: list[str] = []
    for m in FIELD.finditer(template):
        if m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


def render(template: str, record: Mapping) -> str:
    """Fill every field from the record, HTML-escaped."""
    out = template
    for name in fields(template):
        value = escape_html(lookup(record, name))
        out = re.sub(r"\{" + re.escape(name) + r"\}", value, out)
    return out


def preview(template: str, record: Mapping, width: int = 60) -> str:
    """The first `width` characters of the rendered text with tags removed, for a list view."""
    text = re.sub(r"<[^>]+>", "", render(template, record))
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1].rstrip() + "…"
