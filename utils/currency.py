"""Currency formatting and conversion utilities."""

from typing import Any


def _to_currency_input(value: Any) -> str:
    """Normalize mixed value types (str/float/int/None) to clean numeric text."""
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    return str(value)


def normalize_currency(value: Any) -> str:
    """Convert a value to normalized currency string format ($X.XX)."""
    stripped = _to_currency_input(value).replace("$", "").replace(",", "").strip()
    if not stripped:
        return "$0.00"
    try:
        return f"${float(stripped):.2f}"
    except ValueError:
        return "$0.00"


def currency_to_float(value: Any) -> float:
    """Convert a currency string or numeric value to a float value."""
    stripped = _to_currency_input(value).replace("$", "").replace(",", "").strip()
    if not stripped:
        return 0.0
    try:
        return float(stripped)
    except ValueError:
        return 0.0
