"""Date and string formatting utilities."""

from datetime import datetime


def format_date(date_str: str) -> str:
    """Convert ISO date string (YYYY-MM-DD) to display format (MM/DD/YYYY)."""
    if not date_str:
        return ""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").strftime("%m/%d/%Y")
    except ValueError:
        return date_str


def normalize_duration(value: str) -> str:
    """Trim and normalize a duration string."""
    return (value or "").strip()
