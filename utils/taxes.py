from __future__ import annotations

from typing import Any

from utils.currency import currency_to_float, normalize_currency

ITEM_TYPES = ("services", "parts", "labor", "materials", "equipment")
DEFAULT_TANGIBLE_TYPES = {"parts", "materials", "equipment"}
LEGACY_TYPE_MAP = {
    "tax_parts": "parts",
    "tax_repair_labor": "labor",
    "tax_materials": "materials",
    "tax_installation": "services",
    "tax_fabrication": "services",
}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _tax_rate_to_decimal(value: Any) -> float:
    rate = max(0.0, _safe_float(value, 0.0))
    # Tax rates are now entered as percentages (for example 7.5 for 7.5%).
    # Keep backward compatibility with older decimal storage (for example 0.075).
    if rate >= 1.0:
        return rate / 100.0
    return rate


def parse_tax_override(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value

    text = str(value or "").strip().lower()
    if not text:
        return None
    if text in {"true", "1", "yes", "always"}:
        return True
    if text in {"false", "0", "no", "never"}:
        return False
    if text in {"default", "null", "none"}:
        return None
    return None


def _normalize_applies_to(value: Any) -> list[str]:
    if isinstance(value, list):
        raw_values = value
    else:
        raw_values = str(value or "").split(",")

    normalized: list[str] = []
    for item in raw_values:
        candidate = str(item or "").strip().lower()
        if candidate in ITEM_TYPES and candidate not in normalized:
            normalized.append(candidate)
    return normalized


def _normalize_tax_rate_doc(rate_doc: dict[str, Any], fallback_order: int) -> dict[str, Any]:
    name = str(rate_doc.get("name") or "").strip()
    rate = _safe_float(rate_doc.get("rate"), 0.0)
    active = bool(rate_doc.get("active", True))
    applies_to = _normalize_applies_to(rate_doc.get("applies_to") or [])
    agency = str(rate_doc.get("agency") or "").strip()
    quickbooks_tax_code = str(rate_doc.get("quickbooks_tax_code") or "").strip()
    display_order = int(_safe_float(rate_doc.get("display_order"), fallback_order))

    return {
        "name": name,
        "rate": rate,
        "applies_to": applies_to,
        "agency": agency,
        "active": active,
        "display_order": display_order,
        "quickbooks_tax_code": quickbooks_tax_code,
    }


def normalize_business_tax_rates(business_doc: dict[str, Any] | None) -> list[dict[str, Any]]:
    business = business_doc or {}
    tax_rates = business.get("tax_rates")

    normalized: list[dict[str, Any]] = []
    if isinstance(tax_rates, list):
        for index, tax_rate in enumerate(tax_rates):
            if not isinstance(tax_rate, dict):
                continue
            cleaned = _normalize_tax_rate_doc(tax_rate, index)
            if not cleaned["name"]:
                continue
            normalized.append(cleaned)

    if normalized:
        normalized.sort(key=lambda item: item.get("display_order", 0))
        return normalized

    # Legacy fallback while old fields still exist on some businesses.
    for order, (enabled_key, item_type) in enumerate(LEGACY_TYPE_MAP.items()):
        enabled = str(business.get(enabled_key) or "no").strip().lower() == "yes"
        if not enabled:
            continue

        legacy_rate = _safe_float(business.get(f"{enabled_key}_rate"), 0.0)
        if legacy_rate <= 0:
            continue

        normalized.append(
            {
                "name": f"{item_type.title()} Tax",
                "rate": legacy_rate / 100.0,
                "applies_to": [item_type],
                "agency": "",
                "active": True,
                "display_order": order,
                "quickbooks_tax_code": "",
            }
        )

    return normalized


def build_line_item_tax_inputs(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    source = payload or {}
    rows: list[dict[str, Any]] = []

    for service in source.get("services") or []:
        if not isinstance(service, dict):
            continue
        amount = _safe_float(currency_to_float(service.get("standard_price") or service.get("price")))
        rows.append(
            {
                "item_type": "services",
                "amount": max(0.0, amount),
                "tax_override": parse_tax_override(service.get("tax_override")),
            }
        )

    for part in source.get("parts") or []:
        if not isinstance(part, dict):
            continue
        amount = _safe_float(currency_to_float(part.get("unit_cost") or part.get("price")))
        rows.append(
            {
                "item_type": "parts",
                "amount": max(0.0, amount),
                "tax_override": parse_tax_override(part.get("tax_override")),
            }
        )

    for labor in source.get("labors") or []:
        if not isinstance(labor, dict):
            continue
        line_total = labor.get("line_total")
        if line_total in (None, ""):
            hours = _safe_float(labor.get("hours"), 0.0)
            rate = _safe_float(currency_to_float(labor.get("hourly_rate")), 0.0)
            amount = hours * rate
        else:
            amount = _safe_float(currency_to_float(line_total), 0.0)
        rows.append(
            {
                "item_type": "labor",
                "amount": max(0.0, amount),
                "tax_override": parse_tax_override(labor.get("tax_override")),
            }
        )

    for material in source.get("materials") or []:
        if not isinstance(material, dict):
            continue
        line_total = material.get("line_total")
        if line_total in (None, ""):
            quantity = _safe_float(material.get("quantity_used"), 0.0)
            price = _safe_float(currency_to_float(material.get("price")), 0.0)
            amount = quantity * price
        else:
            amount = _safe_float(currency_to_float(line_total), 0.0)
        rows.append(
            {
                "item_type": "materials",
                "amount": max(0.0, amount),
                "tax_override": parse_tax_override(material.get("tax_override")),
            }
        )

    for equipment in source.get("equipments") or []:
        if not isinstance(equipment, dict):
            continue
        line_total = equipment.get("line_total")
        if line_total in (None, ""):
            quantity = _safe_float(equipment.get("quantity_installed"), 0.0)
            price = _safe_float(currency_to_float(equipment.get("price")), 0.0)
            amount = quantity * price
        else:
            amount = _safe_float(currency_to_float(line_total), 0.0)
        rows.append(
            {
                "item_type": "equipment",
                "amount": max(0.0, amount),
                "tax_override": parse_tax_override(equipment.get("tax_override")),
            }
        )

    return rows


def calculate_itemized_tax(line_items: list[dict[str, Any]], tax_rates: list[dict[str, Any]], customer_tax_exempt: bool = False) -> dict[str, Any]:
    if customer_tax_exempt:
        return {
            "tax_lines": [],
            "tax_total": 0.0,
            "is_tax_exempt": True,
            "has_taxable_items": False,
        }

    tax_lines: list[dict[str, Any]] = []
    has_taxable_items = False

    for index, tax_rate in enumerate(tax_rates or []):
        if not isinstance(tax_rate, dict):
            continue

        normalized_rate = _normalize_tax_rate_doc(tax_rate, index)
        if not normalized_rate.get("active"):
            continue

        rate_decimal = _tax_rate_to_decimal(normalized_rate.get("rate"))
        if rate_decimal <= 0:
            continue

        applies_to = set(_normalize_applies_to(normalized_rate.get("applies_to") or []))
        taxable_subtotal = 0.0
        for item in line_items or []:
            if not isinstance(item, dict):
                continue

            amount = max(0.0, _safe_float(item.get("amount"), 0.0))
            if amount <= 0:
                continue

            item_override = parse_tax_override(item.get("tax_override"))
            if item_override is False:
                continue

            item_type = str(item.get("item_type") or "").strip().lower()
            item_is_taxable = bool(item_override is True or item_type in applies_to)
            if not item_is_taxable:
                continue

            has_taxable_items = True
            taxable_subtotal += amount

        tax_amount = round(taxable_subtotal * rate_decimal, 2)
        if tax_amount <= 0:
            continue

        rate_percent = rate_decimal * 100.0
        display_name = f"{normalized_rate['name']} {rate_percent:.3f}%"
        tax_lines.append(
            {
                "name": normalized_rate["name"],
                "display_name": display_name,
                "rate": rate_decimal,
                "rate_percent": rate_percent,
                "amount": tax_amount,
                "amount_display": normalize_currency(tax_amount),
                "quickbooks_tax_code": normalized_rate.get("quickbooks_tax_code") or "",
            }
        )

    tax_total = round(sum(line.get("amount", 0.0) for line in tax_lines), 2)
    return {
        "tax_lines": tax_lines,
        "tax_total": tax_total,
        "is_tax_exempt": False,
        "has_taxable_items": has_taxable_items,
    }
