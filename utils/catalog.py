"""Service, parts, labor, materials, equipment, and discount catalog building utilities."""

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
            "part_ids": [str(pid) for pid in (service.get("part_ids") or []) if pid],
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


def build_labor_catalog(labors):
    """Build a labor catalog dictionary from labor documents."""
    catalog = {}
    for labor in labors:
        labor_description = str(labor.get("labor_description") or "").strip()
        if not labor_description:
            continue

        catalog[labor_description] = {
            "description": labor_description,
            "category": str(labor.get("labor_category") or "").strip(),
            "default_hours": _format_hours_value(labor.get("labor_default_hours", "")),
            "hourly_rate": normalize_currency(labor.get("labor_hourly_rate", 0)),
        }

    return catalog


def build_material_catalog(materials):
    """Build a materials catalog dictionary from material documents."""
    catalog = {}
    for material in materials:
        material_name = str(material.get("material_name") or "").strip()
        if not material_name:
            continue

        catalog[material_name] = {
            "material_name": material_name,
            "category": str(material.get("category") or "").strip(),
            "part_number": str(material.get("part_number") or "").strip(),
            "manufacturer": str(material.get("manufacturer") or "").strip(),
            "default_quantity_used": _format_hours_value(material.get("default_quantity_used", "")),
            "unit_of_measure": str(material.get("unit_of_measure") or "").strip(),
            "price": normalize_currency(material.get("price", 0)),
            "purchase_link": str(material.get("purchase_link") or "").strip(),
        }

    return catalog


def build_equipment_catalog(equipments):
    """Build an equipment catalog dictionary from equipment documents."""
    catalog = {}
    for equipment in equipments:
        equipment_name = str(equipment.get("equipment_name") or "").strip()
        if not equipment_name:
            continue

        catalog[equipment_name] = {
            "equipment_name": equipment_name,
            "manufacturer": str(equipment.get("manufacturer") or "").strip(),
            "category": str(equipment.get("category") or "").strip(),
            "sku": str(equipment.get("sku") or "").strip(),
            "description": str(equipment.get("description") or "").strip(),
            "notes": str(equipment.get("notes") or "").strip(),
            "default_price": normalize_currency(equipment.get("default_price", 0)),
            "default_quantity_installed": _format_hours_value(equipment.get("default_quantity_installed", "")),
        }

    return catalog


def build_discount_catalog(discounts):
    """Build a discount catalog dictionary from discount documents."""
    catalog = {}
    for discount in discounts:
        discount_name = str(discount.get("discount_name") or "").strip()
        if not discount_name:
            continue

        percentage_value = discount.get("discount_percentage", "")
        percentage_text = _format_hours_value(percentage_value)

        catalog[discount_name] = {
            "discount_name": discount_name,
            "discount_category": str(discount.get("discount_category") or "").strip(),
            "discount_percentage": percentage_text,
            "discount_amount": normalize_currency(discount.get("discount_amount", 0)),
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


def build_job_labors_from_form(labor_descriptions, labor_hours, labor_rates, labor_catalog):
    """
    Build a list of labor rows from form input and catalog defaults.

    Returns:
        tuple: (labors_list, total_price)
    """
    labors = []
    total = 0.0

    for index, raw_description in enumerate(labor_descriptions):
        labor_description = (raw_description or "").strip()
        if not labor_description:
            continue

        catalog_entry = labor_catalog.get(labor_description, {})
        entered_hours = labor_hours[index] if index < len(labor_hours) else ""
        entered_rate = labor_rates[index] if index < len(labor_rates) else ""

        normalized_hours = _format_hours_value(
            entered_hours if (entered_hours or "").strip() else catalog_entry.get("default_hours", "")
        )
        normalized_rate = normalize_currency(
            entered_rate if (entered_rate or "").strip() else catalog_entry.get("hourly_rate", "$0.00")
        )

        try:
            hours_value = float(normalized_hours or 0)
        except ValueError:
            hours_value = 0.0
        rate_value = float(normalized_rate.replace("$", "").replace(",", ""))
        row_total = hours_value * rate_value

        labors.append(
            {
                "description": labor_description,
                "category": catalog_entry.get("category", ""),
                "hours": normalized_hours,
                "hourly_rate": normalized_rate,
                "line_total": normalize_currency(row_total),
            }
        )
        total += row_total

    return labors, total


def build_job_materials_from_form(
    material_names,
    material_quantities,
    material_units,
    material_prices,
    material_catalog,
):
    """
    Build a list of material rows from form input and catalog defaults.

    Returns:
        tuple: (materials_list, total_price)
    """
    materials = []
    total = 0.0

    for index, raw_name in enumerate(material_names):
        material_name = (raw_name or "").strip()
        if not material_name:
            continue

        catalog_entry = material_catalog.get(material_name, {})
        entered_quantity = material_quantities[index] if index < len(material_quantities) else ""
        entered_unit = material_units[index] if index < len(material_units) else ""
        entered_price = material_prices[index] if index < len(material_prices) else ""

        quantity = _format_hours_value(
            entered_quantity if (entered_quantity or "").strip() else catalog_entry.get("default_quantity_used", "")
        )
        unit_price = normalize_currency(
            entered_price if (entered_price or "").strip() else catalog_entry.get("price", "$0.00")
        )

        try:
            quantity_value = float(quantity or 0)
        except ValueError:
            quantity_value = 0.0
        price_value = float(unit_price.replace("$", "").replace(",", ""))
        line_total = quantity_value * price_value

        materials.append(
            {
                "material_name": material_name,
                "category": catalog_entry.get("category", ""),
                "part_number": catalog_entry.get("part_number", ""),
                "manufacturer": catalog_entry.get("manufacturer", ""),
                "quantity_used": quantity,
                "unit_of_measure": (entered_unit if (entered_unit or "").strip() else catalog_entry.get("unit_of_measure", "")).strip(),
                "price": unit_price,
                "line_total": normalize_currency(line_total),
            }
        )
        total += line_total

    return materials, total


def build_job_equipments_from_form(
    equipment_names,
    equipment_quantities,
    equipment_prices,
    equipment_catalog,
):
    """
    Build a list of equipment rows from form input and catalog defaults.

    Returns:
        tuple: (equipments_list, total_price)
    """
    equipments = []
    total = 0.0

    for index, raw_name in enumerate(equipment_names):
        equipment_name = (raw_name or "").strip()
        if not equipment_name:
            continue

        catalog_entry = equipment_catalog.get(equipment_name, {})
        entered_quantity = equipment_quantities[index] if index < len(equipment_quantities) else ""
        entered_price = equipment_prices[index] if index < len(equipment_prices) else ""

        quantity = _format_hours_value(
            entered_quantity if (entered_quantity or "").strip() else catalog_entry.get("default_quantity_installed", "")
        )
        unit_price = normalize_currency(
            entered_price if (entered_price or "").strip() else catalog_entry.get("default_price", "$0.00")
        )

        try:
            quantity_value = float(quantity or 0)
        except ValueError:
            quantity_value = 0.0
        price_value = float(unit_price.replace("$", "").replace(",", ""))
        line_total = quantity_value * price_value

        equipments.append(
            {
                "equipment_name": equipment_name,
                "manufacturer": catalog_entry.get("manufacturer", ""),
                "category": catalog_entry.get("category", ""),
                "sku": catalog_entry.get("sku", ""),
                "quantity_installed": quantity,
                "price": unit_price,
                "line_total": normalize_currency(line_total),
            }
        )
        total += line_total

    return equipments, total


def build_job_discounts_from_form(discount_names, discount_percentages, discount_amounts, discount_catalog):
    """
    Build a list of discount rows from form input and catalog defaults.

    Returns:
        tuple: (discounts_list, total_discount_amount)
    """
    discounts = []
    total_discount = 0.0

    for index, raw_name in enumerate(discount_names):
        discount_name = (raw_name or "").strip()
        if not discount_name:
            continue

        catalog_entry = discount_catalog.get(discount_name, {})
        entered_percentage = discount_percentages[index] if index < len(discount_percentages) else ""
        entered_amount = discount_amounts[index] if index < len(discount_amounts) else ""

        percentage = (entered_percentage if (entered_percentage or "").strip() else catalog_entry.get("discount_percentage", "")).strip()
        amount = normalize_currency(
            entered_amount if (entered_amount or "").strip() else catalog_entry.get("discount_amount", "$0.00")
        )

        try:
            amount_value = float(amount.replace("$", "").replace(",", ""))
        except ValueError:
            amount_value = 0.0

        discounts.append(
            {
                "discount_name": discount_name,
                "discount_category": catalog_entry.get("discount_category", ""),
                "discount_percentage": percentage,
                "discount_amount": amount,
                "line_total": f"-{amount}",
            }
        )
        total_discount += amount_value

    return discounts, total_discount
