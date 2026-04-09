"""Service and parts catalog building utilities."""

from utils.currency import normalize_currency


def _format_hours_value(value):
    text = str(value or "").strip()
    if not text:
        return ""

    if text.endswith(".0"):
        text = text[:-2]

    return text


def build_service_catalog(services):
    """Build a service catalog dictionary from service documents."""
    catalog = {}
    for service in services:
        service_code = str(service.get("service_code") or "").strip()
        if not service_code:
            continue

        catalog[service_code] = {
            "name": str(service.get("service_name") or "").strip(),
            "code": service_code,
            "description": str(service.get("description") or "").strip(),
            "price": normalize_currency(service.get("standard_price", 0)),
            "standard_price": normalize_currency(service.get("standard_price", 0)),
            "materials_cost": normalize_currency(service.get("materials_cost", 0)),
            "estimated_hours": _format_hours_value(service.get("estimated_hours", "")),
        }

    return catalog


def build_part_catalog(parts):
    """Build a parts catalog dictionary from part documents."""
    catalog = {}
    for part in parts:
        part_code = str(part.get("part_code") or "").strip()
        if not part_code:
            continue

        catalog[part_code] = {
            "name": str(part.get("part_name") or "").strip(),
            "code": part_code,
            "description": str(part.get("description") or "").strip(),
            "price": normalize_currency(part.get("unit_cost", 0)),
            "unit_cost": normalize_currency(part.get("unit_cost", 0)),
        }

    return catalog


def build_job_services_from_form(service_codes, service_prices, service_durations, service_catalog):
    """
    Build a list of services from form input and catalog defaults.
    
    Returns:
        tuple: (services_list, total_price)
    """
    services = []
    total = 0.0

    for index, raw_service_code in enumerate(service_codes):
        service_code = (raw_service_code or "").strip()
        if not service_code:
            continue

        catalog_entry = service_catalog.get(service_code, {})
        entered_price = service_prices[index] if index < len(service_prices) else ""
        entered_duration = service_durations[index] if index < len(service_durations) else ""

        price = entered_price if (entered_price or "").strip() else catalog_entry.get("price", "$0.00")
        duration = entered_duration if (entered_duration or "").strip() else catalog_entry.get("estimated_hours", "")
        normalized_price = normalize_currency(price)
        normalized_duration = _format_hours_value(duration)
        service_name = catalog_entry.get("name") or service_code

        services.append(
            {
                "type": service_name,
                "code": service_code,
                "price": normalized_price,
                "estimated_hours": normalized_duration,
                "duration": normalized_duration,
            }
        )
        total += float(normalized_price.replace("$", "").replace(",", ""))

    return services, total


def build_job_parts_from_form(part_codes, part_prices, part_catalog):
    """
    Build a list of parts from form input and catalog defaults.
    
    Returns:
        tuple: (parts_list, total_price)
    """
    parts = []
    total = 0.0

    for index, raw_part_code in enumerate(part_codes):
        part_code = (raw_part_code or "").strip()
        if not part_code:
            continue

        catalog_entry = part_catalog.get(part_code, {})
        entered_price = part_prices[index] if index < len(part_prices) else ""
        price = entered_price if (entered_price or "").strip() else catalog_entry.get("price", "$0.00")
        normalized_price = normalize_currency(price)
        part_name = catalog_entry.get("name") or part_code

        parts.append(
            {
                "name": part_name,
                "code": part_code,
                "price": normalized_price,
            }
        )
        total += float(normalized_price.replace("$", "").replace(",", ""))

    return parts, total
