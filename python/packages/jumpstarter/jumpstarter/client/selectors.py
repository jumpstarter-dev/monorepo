"""Kubernetes label selector parsing and matching utilities."""

from __future__ import annotations

import re


def parse_label_selector(selector: str) -> tuple[dict[str, str], list[tuple[str, str, list[str]]]]:
    """Parse a label selector string into matchLabels and matchExpressions.

    Returns (matchLabels, matchExpressions) where matchExpressions is a list of
    (key, operator, values) tuples. Operators: "=", "!=", "in", "notin", "exists", "!exists"
    """
    if not selector or not selector.strip():
        return {}, []

    match_labels: dict[str, str] = {}
    match_expressions: list[tuple[str, str, list[str]]] = []

    # Split by comma, but not inside parentheses
    parts = re.split(r",(?![^()]*\))", selector)

    for part in parts:
        part = part.strip()
        if not part:
            continue

        # key in (v1, v2, ...)
        if m := re.match(r"^([a-zA-Z0-9_./-]+)\s+in\s+\(([^)]*)\)$", part):
            key, values = m.groups()
            match_expressions.append((key, "in", [v.strip() for v in values.split(",")]))
        # key notin (v1, v2, ...)
        elif m := re.match(r"^([a-zA-Z0-9_./-]+)\s+notin\s+\(([^)]*)\)$", part):
            key, values = m.groups()
            match_expressions.append((key, "notin", [v.strip() for v in values.split(",")]))
        # !key (DoesNotExist)
        elif m := re.match(r"^!\s*([a-zA-Z0-9_./-]+)$", part):
            match_expressions.append((m.group(1), "!exists", []))
        # key!=value (whitespace-tolerant)
        elif m := re.match(r"^([a-zA-Z0-9_./-]+)\s*!=\s*(.+)$", part):
            key, value = m.groups()
            match_expressions.append((key, "!=", [value.strip()]))
        # key=value or key==value (whitespace-tolerant)
        elif m := re.match(r"^([a-zA-Z0-9_./-]+)\s*==?\s*(.+)$", part):
            key, value = m.groups()
            match_labels[key] = value.strip()
        # key (Exists) - bare key without operator
        elif re.match(r"^[a-zA-Z0-9_./-]+$", part):
            match_expressions.append((part, "exists", []))

    return match_labels, match_expressions


def selector_contains(selector: str, requirements: str) -> bool:
    """Check if selector contains all criteria from requirements.

    Returns True if all matchLabels and matchExpressions in `requirements`
    are present in `selector`.
    """
    if not requirements or not requirements.strip():
        return True

    req_labels, req_exprs = parse_label_selector(requirements)
    sel_labels, sel_exprs = parse_label_selector(selector)

    # All required matchLabels must be in selector's matchLabels
    for key, value in req_labels.items():
        if sel_labels.get(key) != value:
            return False

    # All required matchExpressions must be in selector's matchExpressions
    for r_key, r_op, r_vals in req_exprs:
        found = False
        for s_key, s_op, s_vals in sel_exprs:
            if s_key == r_key and s_op == r_op and set(s_vals) == set(r_vals):
                found = True
                break
        if not found:
            return False

    return True
