"""Markup rule helpers for parts/materials pricing."""


def get_markup_rule(cost_price, markup_rules):
    """Return first matching rule by ascending range_min; None if no match."""
    try:
        numeric_cost = float(cost_price)
    except (TypeError, ValueError):
        return None

    sorted_rules = sorted(markup_rules or [], key=lambda rule: float(rule.get("range_min", 0) or 0))
    for rule in sorted_rules:
        try:
            range_min = float(rule.get("range_min", 0) or 0)
        except (TypeError, ValueError):
            continue
        range_max_raw = rule.get("range_max")
        try:
            range_max = float(range_max_raw) if range_max_raw is not None else None
        except (TypeError, ValueError):
            continue

        if numeric_cost < range_min:
            continue
        if range_max is None or numeric_cost <= range_max:
            return {
                "range_min": range_min,
                "range_max": range_max,
                "markup_percent": float(rule.get("markup_percent", 0) or 0),
            }
    return None


def calculate_sell_price(cost_price, markup_percent):
    """Apply markup: sell = cost * (1 + markup_percent / 100), rounded to 2 decimals."""
    numeric_cost = float(cost_price)
    numeric_markup = float(markup_percent)
    return round(numeric_cost * (1 + (numeric_markup / 100.0)), 2)


def format_rule_range(rule):
    """Human-readable display for a markup rule range."""
    if not isinstance(rule, dict):
        return "$0.00 - no limit"

    try:
        range_min = float(rule.get("range_min", 0) or 0)
    except (TypeError, ValueError):
        range_min = 0.0

    range_max_raw = rule.get("range_max")
    if range_max_raw is None:
        return f"${range_min:,.2f} - no limit"

    try:
        range_max = float(range_max_raw)
    except (TypeError, ValueError):
        return f"${range_min:,.2f} - no limit"

    return f"${range_min:,.2f} - ${range_max:,.2f}"
