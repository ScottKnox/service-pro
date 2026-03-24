"""Service and parts catalog building utilities."""

from utils.currency import normalize_currency
from utils.formatters import normalize_duration


def build_service_catalog(services):
    """Build a service catalog dictionary from service documents."""
    return {
        service["service_type"]: {
            "price": service.get("service_default_price", "$0.00"),
            "duration": service.get("service_duration", ""),
        }
        for service in services
    }


def build_part_catalog(parts):
    """Build a parts catalog dictionary from parts documents."""
    return {
        part["part_name"]: {
            "price": part.get("part_default_price", "$0.00"),
        }
        for part in parts
    }


def build_job_services_from_form(service_types, service_prices, service_durations, service_catalog):
    """
    Build a list of services from form input and catalog defaults.
    
    Returns:
        tuple: (services_list, total_price)
    """
    services = []
    total = 0.0

    for index, raw_service_type in enumerate(service_types):
        service_type = (raw_service_type or "").strip()
        if not service_type:
            continue

        catalog_entry = service_catalog.get(service_type, {})
        entered_price = service_prices[index] if index < len(service_prices) else ""
        entered_duration = service_durations[index] if index < len(service_durations) else ""

        price = entered_price if (entered_price or "").strip() else catalog_entry.get("price", "$0.00")
        duration = entered_duration if (entered_duration or "").strip() else catalog_entry.get("duration", "")
        normalized_price = normalize_currency(price)
        normalized_duration = normalize_duration(duration)

        services.append(
            {
                "type": service_type,
                "price": normalized_price,
                "duration": normalized_duration,
            }
        )
        total += float(normalized_price.replace("$", "").replace(",", ""))

    return services, total


def build_job_parts_from_form(part_names, part_prices, part_catalog):
    """
    Build a list of parts from form input and catalog defaults.
    
    Returns:
        tuple: (parts_list, total_price)
    """
    parts = []
    total = 0.0

    for index, raw_part_name in enumerate(part_names):
        part_name = (raw_part_name or "").strip()
        if not part_name:
            continue

        catalog_entry = part_catalog.get(part_name, {})
        entered_price = part_prices[index] if index < len(part_prices) else ""
        price = entered_price if (entered_price or "").strip() else catalog_entry.get("price", "$0.00")
        normalized_price = normalize_currency(price)

        parts.append(
            {
                "name": part_name,
                "price": normalized_price,
            }
        )
        total += float(normalized_price.replace("$", "").replace(",", ""))

    return parts, total
