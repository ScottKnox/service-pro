"""Currency formatting and conversion utilities."""


def normalize_currency(value: str) -> str:
    """Convert a value to normalized currency string format ($X.XX)."""
    stripped = (value or "").replace("$", "").replace(",", "").strip()
    if not stripped:
        return "$0.00"
    try:
        return f"${float(stripped):.2f}"
    except ValueError:
        return "$0.00"


def currency_to_float(value: str) -> float:
    """Convert a currency string to a float value."""
    stripped = (value or "").replace("$", "").replace(",", "").strip()
    if not stripped:
        return 0.0
    try:
        return float(stripped)
    except ValueError:
        return 0.0
