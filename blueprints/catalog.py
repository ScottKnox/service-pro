from datetime import UTC, datetime

from bson import ObjectId
from flask import Blueprint, redirect, render_template, request, session, url_for

from mongo import ensure_connection_or_500, object_id_or_404, serialize_doc
from utils.csv_export import build_csv_export_response

bp = Blueprint("catalog", __name__)

SERVICE_TYPE_OPTIONS = [
    "Installation",
    "Repairs",
    "Maintenance / Tune-Up",
    "Diagnostics",
    "Service Agreement / Contracts",
]

MATERIAL_UOM_OPTIONS = [
    "lbs",
    "oz",
    "gal",
    "qt",
    "pt",
    "fl_oz",
    "ft",
    "in",
    "sq_ft",
    "each",
    "box",
    "roll",
]

MATERIAL_UOM_LABELS = {
    "lbs": "lb",
    "oz": "oz",
    "gal": "gal",
    "qt": "qt",
    "pt": "pt",
    "fl_oz": "fl oz",
    "ft": "ft",
    "in": "in",
    "sq_ft": "sq ft",
    "each": "each",
    "box": "box",
    "roll": "roll",
}

MATERIAL_UOM_GROUPS = {
    "lbs": "Weight",
    "oz": "Weight",
    "gal": "Volume",
    "qt": "Volume",
    "pt": "Volume",
    "fl_oz": "Volume",
    "ft": "Length",
    "in": "Length",
    "sq_ft": "Area",
    "each": "Count",
    "box": "Count",
    "roll": "Count",
}

EQUIPMENT_TYPE_OPTIONS = [
    "AC Condenser",
    "Heat Pump Condenser",
    "Gas Furnace",
    "Air Handler",
    "Mini Split Outdoor Unit",
    "Mini Split Indoor Unit",
    "Package Unit",
    "Other",
]

EQUIPMENT_COOLING_CAPACITY_OPTIONS = [
    ".50 Ton (6000 BTU)",
    ".75 Ton (9000 BTU)",
    "1 Ton (12000 BTU)",
    "1.5 Tons (18000 BTU)",
    "2 Ton (24000 BTU)",
    "2.5 Tons (30000 BTU)",
    "3 Ton (36000 BTU)",
    "3.5 Tons (42000 BTU)",
    "4 Ton (48000 BTU)",
    "4.5 Ton (54000 BTU)",
    "5 Ton (60000 BTU)",
]

EQUIPMENT_REFRIGERANT_TYPE_OPTIONS = [
    "R-22",
    "R-410A",
    "R-32",
    "R-454B",
    "R-134a",
    "R-407C",
    "Other",
]

EQUIPMENT_BTU_OPTIONS = list(EQUIPMENT_COOLING_CAPACITY_OPTIONS)

EQUIPMENT_STAGES_OPTIONS = [
    "Single",
    "Two",
    "Modulating",
]

EQUIPMENT_BLOWER_MOTOR_TYPE_OPTIONS = [
    "Standard PSC Motor",
    "ECM Motor",
]


def _format_currency_display(value):
    try:
        numeric = float(value or 0)
    except (TypeError, ValueError):
        numeric = 0.0
    return f"${numeric:,.2f}"


def _format_hours_display(value):
    try:
        numeric = float(value or 0)
    except (TypeError, ValueError):
        return "-"

    if numeric.is_integer():
        return str(int(numeric))
    return f"{numeric:g}"


def _parse_nonnegative_float(raw_value, field_label):
    value_text = str(raw_value or "").strip()
    if not value_text:
        return None, f"{field_label} is required."

    try:
        numeric = float(value_text)
    except ValueError:
        return None, f"{field_label} must be a valid number."

    if numeric < 0:
        return None, f"{field_label} cannot be negative."

    return numeric, ""


def _parse_optional_nonnegative_float(raw_value, field_label, default=0.0):
    value_text = str(raw_value or "").strip()
    if not value_text:
        if default is None:
            return None, ""
        return float(default), ""

    try:
        numeric = float(value_text)
    except ValueError:
        return None, f"{field_label} must be a valid number."

    if numeric < 0:
        return None, f"{field_label} cannot be negative."

    return numeric, ""


def _parse_optional_integer(raw_value, field_label):
    value_text = str(raw_value or "").strip()
    if not value_text:
        return None, ""

    try:
        numeric = int(value_text)
    except ValueError:
        return None, f"{field_label} must be a whole number."

    if numeric < 0:
        return None, f"{field_label} cannot be negative."

    return numeric, ""


def _parse_boolean(raw_value):
    return str(raw_value or "").strip().lower() in {"true", "1", "yes", "on"}


def _parse_tax_override(raw_value):
    value = str(raw_value or "").strip().lower()
    if value in {"true", "always"}:
        return True
    if value in {"false", "never"}:
        return False
    return None


def _resolve_current_business_id(db):
    employee_id = session.get("employee_id")
    if not employee_id or not ObjectId.is_valid(employee_id):
        return None

    employee = db.employees.find_one({"_id": ObjectId(employee_id)}, {"business": 1})
    business_ref = (employee or {}).get("business")
    if isinstance(business_ref, ObjectId):
        return business_ref
    if isinstance(business_ref, str) and ObjectId.is_valid(business_ref):
        return ObjectId(business_ref)
    return None


def _is_authenticated_employee():
    employee_id = session.get("employee_id")
    return bool(employee_id and ObjectId.is_valid(employee_id))


def _employee_has_access_to_catalog_item(db, collection_name, item_id):
    """Check if authenticated employee can access catalog items (services, parts, labor, equipment)."""
    if not _is_authenticated_employee():
        return False

    if not item_id or not ObjectId.is_valid(str(item_id)):
        return False

    employee_business_id = _resolve_current_business_id(db)
    if not employee_business_id:
        return False

    item_oid = ObjectId(item_id) if isinstance(item_id, str) else item_id
    collection = db[collection_name]
    item = collection.find_one(
        {"_id": item_oid},
        {"business_id": 1}
    )
    if not item:
        return False

    item_business_id = item.get("business_id")
    if isinstance(item_business_id, ObjectId):
        return item_business_id == employee_business_id
    if isinstance(item_business_id, str) and ObjectId.is_valid(item_business_id):
        return ObjectId(item_business_id) == employee_business_id
    return False


@bp.before_request
def _enforce_staff_catalog_scope():
    """Guard catalog item routes to prevent cross-business access."""
    if not _is_authenticated_employee():
        return None

    view_args = request.view_args or {}
    service_id = str(view_args.get("serviceId") or "").strip()
    part_id = str(view_args.get("partId") or "").strip()
    labor_id = str(view_args.get("laborId") or "").strip()
    equipment_id = str(view_args.get("equipmentId") or "").strip()

    db = ensure_connection_or_500()

    if service_id and ObjectId.is_valid(service_id):
        if not _employee_has_access_to_catalog_item(db, "services", service_id):
            from flask import current_app
            current_app.logger.warning(
                "Blocked cross-business service access: employee_id=%s service_id=%s",
                str(session.get("employee_id") or ""),
                service_id,
            )
            return redirect(url_for("catalog.manage_services"))

    if part_id and ObjectId.is_valid(part_id):
        if not _employee_has_access_to_catalog_item(db, "parts", part_id):
            from flask import current_app
            current_app.logger.warning(
                "Blocked cross-business part access: employee_id=%s part_id=%s",
                str(session.get("employee_id") or ""),
                part_id,
            )
            return redirect(url_for("catalog.manage_parts"))

    if labor_id and ObjectId.is_valid(labor_id):
        if not _employee_has_access_to_catalog_item(db, "labors", labor_id):
            from flask import current_app
            current_app.logger.warning(
                "Blocked cross-business labor access: employee_id=%s labor_id=%s",
                str(session.get("employee_id") or ""),
                labor_id,
            )
            return redirect(url_for("catalog.manage_labor"))

    if equipment_id and ObjectId.is_valid(equipment_id):
        if not _employee_has_access_to_catalog_item(db, "equipment_options", equipment_id):
            from flask import current_app
            current_app.logger.warning(
                "Blocked cross-business equipment access: employee_id=%s equipment_id=%s",
                str(session.get("employee_id") or ""),
                equipment_id,
            )
            return redirect(url_for("catalog.manage_equipment"))

    return None


def _serialize_service(service):
    serialized = serialize_doc(service)
    service_name = str(serialized.get("service_name") or serialized.get("name") or "").strip()
    service_price = serialized.get("price")
    if service_price is None:
        service_price = serialized.get("standard_price")

    labor_hours = serialized.get("labor_hours")
    if labor_hours is None:
        labor_hours = serialized.get("estimated_hours")

    included_parts = serialized.get("included_parts") if isinstance(serialized.get("included_parts"), list) else []

    if not included_parts:
        service_parts = serialized.get("service_parts") if isinstance(serialized.get("service_parts"), list) else []
        service_materials = serialized.get("service_materials") if isinstance(serialized.get("service_materials"), list) else []
        included_parts = []
        for entry in service_parts:
            if not isinstance(entry, dict):
                continue
            included_parts.append(
                {
                    "part_id": str(entry.get("part_id") or "").strip(),
                    "part_name": "",
                    "subcategory": "part",
                    "quantity": 1,
                    "unit_price": entry.get("unit_cost"),
                }
            )
        for entry in service_materials:
            if not isinstance(entry, dict):
                continue
            included_parts.append(
                {
                    "part_id": str(entry.get("material_id") or "").strip(),
                    "part_name": str(entry.get("material_name") or "").strip(),
                    "subcategory": "material",
                    "quantity": entry.get("quantity") if entry.get("quantity") is not None else (entry.get("default_quantity_used") if entry.get("default_quantity_used") is not None else 1),
                    "unit_price": entry.get("unit_price") if entry.get("unit_price") is not None else entry.get("price"),
                }
            )

    serialized["service_name"] = service_name
    serialized["name"] = service_name
    serialized["service_type"] = str(serialized.get("service_type") or "").strip()
    serialized["price"] = service_price
    serialized["price_display"] = _format_currency_display(service_price)
    serialized["standard_price_display"] = serialized["price_display"]
    serialized["labor_hours"] = labor_hours
    serialized["estimated_hours_display"] = _format_hours_display(labor_hours)
    serialized["labor_rate_override_display"] = _format_currency_display(serialized.get("labor_rate_override")) if serialized.get("labor_rate_override") is not None else "-"
    serialized["show_labor_breakdown"] = _parse_boolean(serialized.get("show_labor_breakdown"))
    serialized["show_labor_breakdown_display"] = "Yes" if serialized["show_labor_breakdown"] else "No"
    serialized["included_parts"] = included_parts
    serialized["included_parts_count"] = len(included_parts)
    serialized["is_active"] = bool(serialized.get("is_active", True))
    return serialized


def _build_service_part_rows(selected_part_ids=None, entered_costs=None, service_part_entries=None):
    rows = []
    selected_part_ids = selected_part_ids or []
    entered_costs = entered_costs or []
    service_part_entries = service_part_entries or []

    if service_part_entries:
        for entry in service_part_entries:
            rows.append(
                {
                    "part_id": str(entry.get("part_id") or "").strip(),
                    "unit_cost": _format_currency_display(entry.get("unit_cost")),
                }
            )
        return rows

    for index, part_id in enumerate(selected_part_ids):
        rows.append(
            {
                "part_id": str(part_id or "").strip(),
                "unit_cost": str(entered_costs[index] or "").strip() if index < len(entered_costs) else "",
            }
        )

    return rows or [{"part_id": "", "unit_cost": ""}]


def _build_service_material_rows(selected_material_ids=None, entered_quantities=None, entered_units=None, entered_prices=None, service_material_entries=None):
    rows = []
    selected_material_ids = selected_material_ids or []
    entered_quantities = entered_quantities or []
    entered_units = entered_units or []
    entered_prices = entered_prices or []
    service_material_entries = service_material_entries or []

    if service_material_entries:
        for entry in service_material_entries:
            quantity = entry.get("quantity") if entry.get("quantity") is not None else entry.get("default_quantity_used")
            unit_price = entry.get("unit_price") if entry.get("unit_price") is not None else entry.get("price")
            rows.append(
                {
                    "material_id": str(entry.get("material_id") or "").strip(),
                    "default_quantity_used": _format_hours_display(quantity) if quantity is not None else "",
                    "unit_of_measure": str(entry.get("unit_of_measure") or "").strip(),
                    "unit_label": str(entry.get("unit_label") or "").strip(),
                    "price": _format_currency_display(unit_price),
                }
            )
        return rows

    for index, material_id in enumerate(selected_material_ids):
        rows.append(
            {
                "material_id": str(material_id or "").strip(),
                "default_quantity_used": str(entered_quantities[index] or "").strip() if index < len(entered_quantities) else "",
                "unit_of_measure": str(entered_units[index] or "").strip() if index < len(entered_units) else "",
                "unit_label": "",
                "price": str(entered_prices[index] or "").strip() if index < len(entered_prices) else "",
            }
        )

    return rows or [{"material_id": "", "default_quantity_used": "", "unit_of_measure": "", "unit_label": "", "price": ""}]


def _parse_service_part_entries(part_ids, part_costs):
    entries = []
    seen_part_ids = []

    for index, part_id in enumerate(part_ids or []):
        normalized_part_id = str(part_id or "").strip()
        if not normalized_part_id:
            continue
        if not ObjectId.is_valid(normalized_part_id):
            return None, None, "Please select a valid part."

        raw_cost = part_costs[index] if index < len(part_costs) else ""
        unit_cost, error = _parse_optional_nonnegative_float(raw_cost, "Part Unit Cost")
        if error:
            return None, None, error

        part_oid = ObjectId(normalized_part_id)
        seen_part_ids.append(part_oid)
        entries.append({"part_id": part_oid, "unit_cost": unit_cost})

    return entries, seen_part_ids, ""


def _parse_service_material_entries(material_ids, quantities, units, prices):
    entries = []
    seen_material_ids = []

    for index, material_id in enumerate(material_ids or []):
        normalized_material_id = str(material_id or "").strip()
        if not normalized_material_id:
            continue
        if not ObjectId.is_valid(normalized_material_id):
            return None, None, "Please select a valid material."

        raw_quantity = quantities[index] if index < len(quantities) else ""
        raw_unit = units[index] if index < len(units) else ""
        raw_price = prices[index] if index < len(prices) else ""

        quantity, error = _parse_optional_nonnegative_float(raw_quantity, "Material Quantity", default=None)
        if error:
            return None, None, error

        price, error = _parse_optional_nonnegative_float(raw_price, "Material Price")
        if error:
            return None, None, error

        material_oid = ObjectId(normalized_material_id)
        seen_material_ids.append(material_oid)
        entries.append(
            {
                "material_id": material_oid,
                "quantity": quantity,
                "default_quantity_used": quantity,
                "unit_of_measure": str(raw_unit or "").strip(),
                "unit_label": "",
                "unit_price": price,
                "price": price,
            }
        )

    return entries, seen_material_ids, ""


def _serialize_part(part):
    serialized = serialize_doc(part)
    part_name = str(serialized.get("part_name") or serialized.get("name") or "").strip()
    cost_price = serialized.get("cost_price")
    if cost_price is None:
        cost_price = serialized.get("unit_cost")
    sell_price = serialized.get("sell_price")
    if sell_price is None:
        sell_price = serialized.get("unit_cost")

    serialized["category"] = str(serialized.get("category") or "").strip()
    serialized["subcategory"] = "part"
    serialized["part_name"] = part_name
    serialized["name"] = part_name
    serialized["sku"] = str(serialized.get("sku") or "").strip()
    serialized["cost_price"] = cost_price
    serialized["sell_price"] = sell_price
    serialized["cost_price_display"] = _format_currency_display(cost_price)
    serialized["sell_price_display"] = _format_currency_display(sell_price)
    serialized["unit_cost_display"] = serialized["sell_price_display"]
    serialized["unit_cost"] = sell_price
    warranty_months = serialized.get("warranty_months")
    serialized["warranty_months"] = warranty_months if warranty_months is not None else ""
    markup_percent = None
    try:
        cost_numeric = float(cost_price)
        sell_numeric = float(sell_price)
        if cost_numeric > 0:
            markup_percent = ((sell_numeric - cost_numeric) / cost_numeric) * 100
    except (TypeError, ValueError):
        markup_percent = None
    serialized["markup_percent"] = markup_percent
    serialized["markup_percent_display"] = f"{markup_percent:.1f}%" if markup_percent is not None else "-"
    serialized["is_active"] = bool(serialized.get("is_active", True))
    serialized["purchase_link"] = str(serialized.get("purchase_link") or "").strip()
    return serialized


def _serialize_labor(labor):
    serialized = serialize_doc(labor)
    serialized["labor_category"] = str(serialized.get("labor_category") or "").strip()
    serialized["labor_default_hours_display"] = _format_hours_display(serialized.get("labor_default_hours"))
    serialized["labor_hourly_rate_display"] = _format_currency_display(serialized.get("labor_hourly_rate"))
    return serialized


def _serialize_material(material):
    serialized = serialize_doc(material)
    material_name = str(serialized.get("material_name") or serialized.get("name") or "").strip()
    unit_of_measure = str(serialized.get("unit_of_measure") or "").strip()
    unit_label = str(serialized.get("unit_label") or "").strip()
    if not unit_label and unit_of_measure:
        short_unit = MATERIAL_UOM_LABELS.get(unit_of_measure, unit_of_measure)
        unit_label = f"per {short_unit}"

    cost_price_per_unit = serialized.get("cost_price_per_unit")
    if cost_price_per_unit is None:
        cost_price_per_unit = serialized.get("cost_price")
    sell_price_per_unit = serialized.get("sell_price_per_unit")
    if sell_price_per_unit is None:
        sell_price_per_unit = serialized.get("price")

    serialized["category"] = str(serialized.get("category") or "").strip()
    serialized["material_name"] = material_name
    serialized["name"] = material_name
    serialized["description"] = str(serialized.get("description") or "").strip()
    serialized["part_number"] = str(serialized.get("part_number") or "").strip()
    serialized["manufacturer"] = str(serialized.get("manufacturer") or "").strip()
    default_quantity = serialized.get("default_quantity_used")
    if default_quantity is None:
        default_quantity = serialized.get("minimum_quantity")
    serialized["default_quantity_used"] = str(default_quantity or "").strip()
    serialized["default_quantity_used_display"] = _format_hours_display(default_quantity)
    serialized["unit_of_measure"] = unit_of_measure
    serialized["unit_label"] = unit_label
    serialized["unit_group"] = MATERIAL_UOM_GROUPS.get(unit_of_measure, "Other")
    serialized["minimum_quantity"] = serialized.get("minimum_quantity")
    serialized["minimum_quantity_display"] = _format_hours_display(serialized.get("minimum_quantity")) if serialized.get("minimum_quantity") is not None else "-"
    serialized["cost_price_per_unit"] = cost_price_per_unit
    serialized["sell_price_per_unit"] = sell_price_per_unit
    serialized["cost_price_per_unit_display"] = _format_currency_display(cost_price_per_unit)
    serialized["sell_price_per_unit_display"] = _format_currency_display(sell_price_per_unit)
    serialized["price"] = sell_price_per_unit
    serialized["price_display"] = serialized["sell_price_per_unit_display"]
    markup_percent = None
    try:
        cost_numeric = float(cost_price_per_unit)
        sell_numeric = float(sell_price_per_unit)
        if cost_numeric > 0:
            markup_percent = ((sell_numeric - cost_numeric) / cost_numeric) * 100
    except (TypeError, ValueError):
        markup_percent = None
    serialized["markup_percent"] = markup_percent
    serialized["markup_percent_display"] = f"{markup_percent:.1f}%" if markup_percent is not None else "-"
    serialized["is_active"] = bool(serialized.get("is_active", True))
    sort_order = serialized.get("sort_order")
    serialized["sort_order"] = sort_order if sort_order is not None else ""
    serialized["purchase_link"] = str(serialized.get("purchase_link") or "").strip()
    return serialized


def _serialize_equipment(equipment):
    serialized = serialize_doc(equipment)
    cost_price = serialized.get("cost_price")
    sell_price = serialized.get("sell_price")
    if cost_price is None:
        cost_price = serialized.get("default_cost_price")
    if sell_price is None:
        sell_price = serialized.get("default_price")

    serialized["equipment_name"] = str(serialized.get("equipment_name") or "").strip()
    serialized["equipment_type"] = str(serialized.get("equipment_type") or "").strip()
    serialized["manufacturer"] = str(serialized.get("manufacturer") or "").strip()
    serialized["category"] = str(serialized.get("category") or "").strip()
    serialized["sku"] = str(serialized.get("sku") or "").strip()
    serialized["model_number"] = str(serialized.get("model_number") or "").strip()
    serialized["description"] = str(serialized.get("description") or "").strip()
    serialized["purchase_link"] = str(serialized.get("purchase_link") or "").strip()
    serialized["cost_price"] = cost_price
    serialized["sell_price"] = sell_price
    serialized["cost_price_display"] = _format_currency_display(cost_price)
    serialized["sell_price_display"] = _format_currency_display(sell_price)
    serialized["warranty_months"] = serialized.get("warranty_months") if serialized.get("warranty_months") is not None else ""
    serialized["is_active"] = bool(serialized.get("is_active", True))
    sort_order = serialized.get("sort_order")
    serialized["sort_order"] = sort_order if sort_order is not None else ""

    serialized["cooling_capacity"] = str(serialized.get("cooling_capacity") or "").strip()
    serialized["seer_rating"] = str(serialized.get("seer_rating") or "").strip()
    serialized["metering_device"] = str(serialized.get("metering_device") or "").strip()
    serialized["afue_rating"] = str(serialized.get("afue_rating") or "").strip()
    serialized["btu_input"] = str(serialized.get("btu_input") or "").strip()
    serialized["btu_output"] = str(serialized.get("btu_output") or "").strip()
    serialized["refrigerant_type"] = str(serialized.get("refrigerant_type") or "").strip()
    serialized["stages"] = str(serialized.get("stages") or "").strip()
    serialized["blower_motor_type"] = str(serialized.get("blower_motor_type") or "").strip()
    serialized["voltage"] = str(serialized.get("voltage") or "").strip()
    return serialized


def _serialize_discount(discount):
    serialized = serialize_doc(discount)
    serialized["discount_category"] = str(serialized.get("discount_category") or "").strip()
    serialized["discount_percentage_display"] = _format_hours_display(serialized.get("discount_percentage"))
    serialized["discount_amount_display"] = _format_currency_display(serialized.get("discount_amount"))
    return serialized


def _build_filter_values(items, key):
    values = sorted({str(item.get(key) or "").strip() for item in items if str(item.get(key) or "").strip()})
    return values


def _build_category_options(db, collection_name, field_name, business_id):
    if not business_id:
        return []

    values = db[collection_name].distinct(field_name, {"business_id": business_id})
    cleaned = sorted({str(value or "").strip() for value in values if str(value or "").strip()})
    return cleaned


CATEGORY_TYPE_CONFIG = {
    "part": {
        "collection_name": "parts",
        "field_name": "category",
        "manage_route": "catalog.manage_parts",
        "item_label_singular": "part",
        "item_label_plural": "parts",
        "section_title": "Part Categories",
    },
    "material": {
        "collection_name": "materials",
        "field_name": "category",
        "manage_route": "catalog.manage_materials",
        "item_label_singular": "material",
        "item_label_plural": "materials",
        "section_title": "Material Categories",
    },
    "equipment": {
        "collection_name": "equipment",
        "field_name": "category",
        "manage_route": "catalog.manage_equipment",
        "item_label_singular": "equipment item",
        "item_label_plural": "equipment items",
        "section_title": "Equipment Categories",
    },
}


def _get_category_type_config(category_type):
    return CATEGORY_TYPE_CONFIG.get(str(category_type or "").strip())


def _get_managed_categories(db, category_type, business_id):
    if not business_id or category_type not in CATEGORY_TYPE_CONFIG:
        return []

    categories = db.categories.find({"company_id": str(business_id), "type": category_type}).sort([("is_default", -1), ("sort_order", 1), ("name", 1)])
    cleaned = []
    for category in categories:
        serialized = serialize_doc(category)
        serialized["name"] = str(serialized.get("name") or "").strip()
        if not serialized["name"]:
            continue
        serialized["is_default"] = bool(serialized.get("is_default"))
        serialized["sort_order"] = int(serialized.get("sort_order") or 0)
        cleaned.append(serialized)
    return cleaned


def _build_managed_category_options(db, category_type, business_id):
    return [category["name"] for category in _get_managed_categories(db, category_type, business_id)]


def _category_exists(db, category_type, business_id, category_name):
    if not business_id:
        return False

    normalized_name = str(category_name or "").strip()
    if not normalized_name:
        return False

    return db.categories.count_documents({"company_id": str(business_id), "type": category_type, "name": normalized_name}, limit=1) > 0


def _custom_category_exists(db, category_type, business_id, category_name):
    if not business_id:
        return False

    normalized_name = str(category_name or "").strip()
    if not normalized_name:
        return False

    return db.categories.count_documents({"company_id": str(business_id), "type": category_type, "name": normalized_name, "is_default": False}, limit=1) > 0


def _build_category_management_context(db, category_type, business_id):
    config = _get_category_type_config(category_type)
    if not config:
        return None

    categories = _get_managed_categories(db, category_type, business_id)
    return {
        "category_type": category_type,
        "section_title": config["section_title"],
        "item_label_singular": config["item_label_singular"],
        "item_label_plural": config["item_label_plural"],
        "default_categories": [category for category in categories if category.get("is_default")],
        "custom_categories": [category for category in categories if not category.get("is_default")],
        "all_categories": categories,
    }


def _remove_category_references_from_services(db, category_type, business_id, category_id):
    if not business_id:
        return

    array_field = {
        "part": "associated_part_category_ids",
        "material": "associated_material_category_ids",
        "equipment": "associated_equipment_category_ids",
    }.get(category_type)
    if not array_field:
        return

    db.services.update_many({"business_id": business_id}, {"$pull": {array_field: category_id}})


def _get_business_markup_rules(db, business_id):
    if not business_id:
        return []

    business = db.businesses.find_one({"_id": business_id}, {"markup_rules": 1}) or {}
    rules = business.get("markup_rules")
    if not isinstance(rules, list):
        return []

    cleaned = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        try:
            range_min = float(rule.get("range_min") or 0)
            markup_percent = float(rule.get("markup_percent") or 0)
        except (TypeError, ValueError):
            continue
        range_max_raw = rule.get("range_max")
        range_max = None
        if range_max_raw not in [None, ""]:
            try:
                range_max = float(range_max_raw)
            except (TypeError, ValueError):
                continue
        cleaned.append(
            {
                "range_min": range_min,
                "range_max": range_max,
                "markup_percent": markup_percent,
            }
        )

    cleaned.sort(key=lambda row: row.get("range_min", 0))
    return cleaned


def _service_form_data(service=None):
    service = service or {}

    labor_hours = service.get("labor_hours")
    if labor_hours is None:
        labor_hours = service.get("estimated_hours")

    price = service.get("price")
    if price is None:
        price = service.get("standard_price")

    return {
        "service_name": str(service.get("service_name") or service.get("name") or "").strip(),
        "service_type": str(service.get("service_type") or "").strip(),
        "category": str(service.get("category") or "").strip(),
        "description": str(service.get("description") or "").strip(),
        "labor_hours": str(labor_hours or "").strip(),
        "labor_rate_override": str(service.get("labor_rate_override") or "").strip(),
        "price": str(price or "").strip(),
        "show_labor_breakdown": "true" if _parse_boolean(service.get("show_labor_breakdown")) else "false",
        "tax_override": "always" if service.get("tax_override") is True else ("never" if service.get("tax_override") is False else "default"),
    }


def _service_associated_category_ids(service=None):
    service = service or {}

    return {
        "associated_part_category_ids": [str(category_id) for category_id in service.get("associated_part_category_ids", []) if str(category_id).strip()],
        "associated_material_category_ids": [str(category_id) for category_id in service.get("associated_material_category_ids", []) if str(category_id).strip()],
        "associated_equipment_category_ids": [str(category_id) for category_id in service.get("associated_equipment_category_ids", []) if str(category_id).strip()],
    }


def _stringify_category_id_list(category_ids):
    return [str(category_id) for category_id in (category_ids or []) if str(category_id).strip()]


def _normalize_selected_category_ids(db, business_id, category_type, selected_category_ids):
    normalized_ids = []
    for raw_category_id in selected_category_ids or []:
        category_id = str(raw_category_id or "").strip()
        if not category_id:
            continue
        if not ObjectId.is_valid(category_id):
            return None, "Please select valid categories."
        normalized_ids.append(ObjectId(category_id))

    if not normalized_ids:
        return [], ""

    categories = db.categories.find(
        {
            "_id": {"$in": normalized_ids},
            "company_id": str(business_id),
            "type": category_type,
        },
        {"_id": 1},
    )
    valid_ids = {category["_id"] for category in categories}
    if len(valid_ids) != len(normalized_ids):
        return None, "Please select valid categories."

    return normalized_ids, ""


def _part_form_data(part=None):
    part = part or {}
    cost_price = part.get("cost_price")
    if cost_price is None:
        cost_price = part.get("unit_cost")
    sell_price = part.get("sell_price")
    if sell_price is None:
        sell_price = part.get("unit_cost")

    return {
        "part_name": str(part.get("part_name") or part.get("name") or "").strip(),
        "category": str(part.get("category") or "").strip(),
        "sku": str(part.get("sku") or "").strip(),
        "description": str(part.get("description") or "").strip(),
        "cost_price": str(cost_price or "").strip(),
        "sell_price": str(sell_price or "").strip(),
        "warranty_months": str(part.get("warranty_months") or "").strip(),
        "purchase_link": str(part.get("purchase_link") or "").strip(),
        "sell_price_auto_populated": "true" if _parse_boolean(part.get("sell_price_auto_populated")) else "false",
        "tax_override": "always" if part.get("tax_override") is True else ("never" if part.get("tax_override") is False else "default"),
    }


def _labor_form_data(labor=None):
    labor = labor or {}
    return {
        "labor_description": str(labor.get("labor_description") or "").strip(),
        "labor_category": str(labor.get("labor_category") or "").strip(),
        "labor_default_hours": str(labor.get("labor_default_hours") or "").strip(),
        "labor_hourly_rate": str(labor.get("labor_hourly_rate") or "").strip(),
        "tax_override": "always" if labor.get("tax_override") is True else ("never" if labor.get("tax_override") is False else "default"),
    }


def _material_form_data(material=None):
    material = material or {}
    unit_of_measure = str(material.get("unit_of_measure") or "").strip()
    unit_label = str(material.get("unit_label") or "").strip()
    if not unit_label and unit_of_measure:
        short_unit = MATERIAL_UOM_LABELS.get(unit_of_measure, unit_of_measure)
        unit_label = f"per {short_unit}"

    cost_price_per_unit = material.get("cost_price_per_unit")
    if cost_price_per_unit is None:
        cost_price_per_unit = material.get("cost_price")
    sell_price_per_unit = material.get("sell_price_per_unit")
    if sell_price_per_unit is None:
        sell_price_per_unit = material.get("price")

    return {
        "material_name": str(material.get("material_name") or "").strip(),
        "description": str(material.get("description") or "").strip(),
        "category": str(material.get("category") or "").strip(),
        "part_number": str(material.get("part_number") or "").strip(),
        "manufacturer": str(material.get("manufacturer") or "").strip(),
        "default_quantity_used": str(material.get("default_quantity_used") or "").strip(),
        "unit_of_measure": unit_of_measure,
        "unit_label": unit_label,
        "cost_price_per_unit": str(cost_price_per_unit or "").strip(),
        "sell_price_per_unit": str(sell_price_per_unit or "").strip(),
        "minimum_quantity": str(material.get("minimum_quantity") or "").strip(),
        "is_active": "true" if bool(material.get("is_active", True)) else "false",
        "sort_order": str(material.get("sort_order") or "").strip(),
        "price": str(sell_price_per_unit or material.get("price") or "").strip(),
        "purchase_link": str(material.get("purchase_link") or "").strip(),
        "sell_price_auto_populated": "true" if _parse_boolean(material.get("sell_price_auto_populated")) else "false",
        "tax_override": "always" if material.get("tax_override") is True else ("never" if material.get("tax_override") is False else "default"),
    }


def _equipment_form_data(equipment=None):
    equipment = equipment or {}
    cost_price = equipment.get("cost_price")
    if cost_price is None:
        cost_price = equipment.get("default_cost_price")

    sell_price = equipment.get("sell_price")
    if sell_price is None:
        sell_price = equipment.get("default_price")

    return {
        "equipment_name": str(equipment.get("equipment_name") or "").strip(),
        "equipment_type": str(equipment.get("equipment_type") or "").strip(),
        "manufacturer": str(equipment.get("manufacturer") or "").strip(),
        "category": str(equipment.get("category") or "").strip(),
        "sku": str(equipment.get("sku") or "").strip(),
        "model_number": str(equipment.get("model_number") or "").strip(),
        "description": str(equipment.get("description") or "").strip(),
        "purchase_link": str(equipment.get("purchase_link") or "").strip(),
        "cost_price": str(cost_price or "").strip(),
        "sell_price": str(sell_price or "").strip(),
        "warranty_months": str(equipment.get("warranty_months") or "").strip(),
        "sort_order": str(equipment.get("sort_order") or "").strip(),
        "is_active": "true" if bool(equipment.get("is_active", True)) else "false",
        "cooling_capacity": str(equipment.get("cooling_capacity") or "").strip(),
        "seer_rating": str(equipment.get("seer_rating") or "").strip(),
        "metering_device": str(equipment.get("metering_device") or "").strip(),
        "afue_rating": str(equipment.get("afue_rating") or "").strip(),
        "btu_input": str(equipment.get("btu_input") or "").strip(),
        "btu_output": str(equipment.get("btu_output") or "").strip(),
        "refrigerant_type": str(equipment.get("refrigerant_type") or "").strip(),
        "stages": str(equipment.get("stages") or "").strip(),
        "blower_motor_type": str(equipment.get("blower_motor_type") or "").strip(),
        "voltage": str(equipment.get("voltage") or "").strip(),
        "tax_override": "always" if equipment.get("tax_override") is True else ("never" if equipment.get("tax_override") is False else "default"),
    }


def _discount_form_data(discount=None):
    discount = discount or {}
    return {
        "discount_name": str(discount.get("discount_name") or "").strip(),
        "discount_category": str(discount.get("discount_category") or "").strip(),
        "discount_percentage": str(discount.get("discount_percentage") or "").strip(),
        "discount_amount": str(discount.get("discount_amount") or "").strip(),
    }


def _material_uom_options():
    return list(MATERIAL_UOM_OPTIONS)


def _build_service_equipment_rows(selected_equipment_ids=None, service_equipment_entries=None):
    rows = []
    if service_equipment_entries:
        for entry in service_equipment_entries:
            rows.append({"equipment_id": str(entry.get("equipment_id") or "")})
        return rows or [{"equipment_id": ""}]
    if selected_equipment_ids is not None:
        if not selected_equipment_ids:
            return [{"equipment_id": ""}]
        for eid in selected_equipment_ids:
            rows.append({"equipment_id": str(eid or "").strip()})
        return rows or [{"equipment_id": ""}]
    return [{"equipment_id": ""}]


def _parse_service_equipment_entries(equipment_ids):
    entries = []
    seen_equipment_ids = []
    for eid in (equipment_ids or []):
        normalized_eid = str(eid or "").strip()
        if not normalized_eid:
            continue
        if not ObjectId.is_valid(normalized_eid):
            return None, None, "Please select a valid equipment item."
        equipment_oid = ObjectId(normalized_eid)
        seen_equipment_ids.append(equipment_oid)
        entries.append({"equipment_id": equipment_oid})
    return entries, seen_equipment_ids, ""


@bp.route("/price-book")
def manage_price_book():
    return render_template("services/manage_price_book.html")


@bp.route("/services")
def manage_services():
    db = ensure_connection_or_500()
    business_id = _resolve_current_business_id(db)
    query = {"business_id": business_id} if business_id else {"_id": None}
    services = [_serialize_service(service) for service in db.services.find(query).sort("name", 1)]

    return render_template(
        "services/manage_services.html",
        services=services,
        service_types=_build_filter_values(services, "service_type"),
    )


@bp.route("/services/export/csv")
def export_services_csv():
    db = ensure_connection_or_500()
    business_id = _resolve_current_business_id(db)
    query = {"business_id": business_id} if business_id else {"_id": None}
    rows = list(db.services.find(query).sort("name", 1))
    return build_csv_export_response(rows, "services_export.csv")


@bp.route("/parts")
def manage_parts():
    db = ensure_connection_or_500()
    business_id = _resolve_current_business_id(db)
    query = {"business_id": business_id} if business_id else {"_id": None}
    parts = [_serialize_part(part) for part in db.parts.find(query).sort("name", 1)]

    return render_template(
        "services/manage_parts.html",
        parts=parts,
        part_categories=_build_managed_category_options(db, "part", business_id),
        category_management=_build_category_management_context(db, "part", business_id),
        category_error=request.args.get("category_error", ""),
    )


@bp.route("/parts/export/csv")
def export_parts_csv():
    db = ensure_connection_or_500()
    business_id = _resolve_current_business_id(db)
    query = {"business_id": business_id} if business_id else {"_id": None}
    rows = list(db.parts.find(query).sort("name", 1))
    return build_csv_export_response(rows, "parts_export.csv", excluded_fields={"manufacturer", "model_number"})


@bp.route("/labor")
def manage_labor():
    db = ensure_connection_or_500()
    business_id = _resolve_current_business_id(db)
    query = {"business_id": business_id} if business_id else {"_id": None}
    labors = [_serialize_labor(labor) for labor in db.labors.find(query).sort("labor_description", 1)]

    return render_template(
        "services/manage_labor.html",
        labors=labors,
        labor_categories=_build_filter_values(labors, "labor_category"),
    )


@bp.route("/labor/export/csv")
def export_labor_csv():
    db = ensure_connection_or_500()
    business_id = _resolve_current_business_id(db)
    query = {"business_id": business_id} if business_id else {"_id": None}
    rows = list(db.labors.find(query).sort("labor_description", 1))
    return build_csv_export_response(rows, "labor_export.csv")


@bp.route("/materials")
def manage_materials():
    db = ensure_connection_or_500()
    business_id = _resolve_current_business_id(db)
    query = {"business_id": business_id} if business_id else {"_id": None}
    materials = [_serialize_material(material) for material in db.materials.find(query).sort([("sort_order", 1), ("material_name", 1)])]

    material_uom_group_filters = [
        {"label": "Weight", "value": "Weight"},
        {"label": "Volume", "value": "Volume"},
        {"label": "Length", "value": "Length"},
        {"label": "Area", "value": "Area"},
        {"label": "Count", "value": "Count"},
    ]

    return render_template(
        "services/manage_materials.html",
        materials=materials,
        material_categories=_build_managed_category_options(db, "material", business_id),
        material_uom_group_filters=material_uom_group_filters,
        category_management=_build_category_management_context(db, "material", business_id),
        category_error=request.args.get("category_error", ""),
    )


@bp.route("/materials/export/csv")
def export_materials_csv():
    db = ensure_connection_or_500()
    business_id = _resolve_current_business_id(db)
    query = {"business_id": business_id} if business_id else {"_id": None}
    rows = list(db.materials.find(query).sort("material_name", 1))
    return build_csv_export_response(rows, "materials_export.csv")


@bp.route("/equipment")
def manage_equipment():
    db = ensure_connection_or_500()
    business_id = _resolve_current_business_id(db)
    query = {"business_id": business_id} if business_id else {"_id": None}
    equipment_items = [_serialize_equipment(equipment) for equipment in db.equipment.find(query).sort("equipment_name", 1)]

    return render_template(
        "services/manage_equipment.html",
        equipment_items=equipment_items,
        equipment_categories=_build_managed_category_options(db, "equipment", business_id),
        equipment_manufacturers=_build_filter_values(equipment_items, "manufacturer"),
        category_management=_build_category_management_context(db, "equipment", business_id),
        category_error=request.args.get("category_error", ""),
    )


@bp.route("/categories/<categoryType>/create", methods=["POST"])
def create_category(categoryType):
    db = ensure_connection_or_500()
    business_id = _resolve_current_business_id(db)
    config = _get_category_type_config(categoryType)
    if not business_id or not config:
        return redirect(url_for("catalog.manage_price_book"))

    category_name = str(request.form.get("category") or "").strip()
    if not category_name:
        return redirect(url_for(config["manage_route"], category_error="Category name is required."))

    if _custom_category_exists(db, categoryType, business_id, category_name):
        return redirect(url_for(config["manage_route"], category_error="That custom category already exists."))

    existing_categories = _get_managed_categories(db, categoryType, business_id)
    next_sort_order = 0
    if existing_categories:
        next_sort_order = max(int(category.get("sort_order") or 0) for category in existing_categories) + 1

    db.categories.insert_one(
        {
            "company_id": str(business_id),
            "type": categoryType,
            "name": category_name,
            "is_default": False,
            "sort_order": next_sort_order,
            "created_at": datetime.now(UTC),
        }
    )
    return redirect(url_for(config["manage_route"]))


@bp.route("/categories/<categoryType>/<categoryId>/delete", methods=["POST"])
def delete_category(categoryType, categoryId):
    db = ensure_connection_or_500()
    business_id = _resolve_current_business_id(db)
    config = _get_category_type_config(categoryType)
    if not business_id or not config or not ObjectId.is_valid(categoryId):
        return redirect(url_for("catalog.manage_price_book"))

    category = db.categories.find_one({"_id": ObjectId(categoryId), "company_id": str(business_id), "type": categoryType})
    if not category:
        return redirect(url_for(config["manage_route"], category_error="That category could not be found."))
    if bool(category.get("is_default")):
        return redirect(url_for(config["manage_route"], category_error="Default categories cannot be deleted."))

    category_name = str(category.get("name") or "").strip()
    if not category_name:
        return redirect(url_for(config["manage_route"], category_error="That category is invalid."))

    collection_name = config["collection_name"]
    field_name = config["field_name"]
    in_use_count = db[collection_name].count_documents({"business_id": business_id, field_name: category_name})
    if in_use_count > 0:
        item_label = config["item_label_plural"] if in_use_count != 1 else config["item_label_singular"]
        error_message = f"Cannot delete {category_name}. {in_use_count} {item_label} are currently using this category. Reassign them before deleting."
        return redirect(url_for(config["manage_route"], category_error=error_message))

    db.categories.delete_one({"_id": ObjectId(categoryId), "company_id": str(business_id), "type": categoryType})
    _remove_category_references_from_services(db, categoryType, business_id, categoryId)
    return redirect(url_for(config["manage_route"]))


@bp.route("/equipment/export/csv")
def export_equipment_csv():
    db = ensure_connection_or_500()
    business_id = _resolve_current_business_id(db)
    query = {"business_id": business_id} if business_id else {"_id": None}
    rows = list(db.equipment.find(query).sort("equipment_name", 1))
    return build_csv_export_response(rows, "equipment_export.csv")


@bp.route("/discounts")
def manage_discounts():
    db = ensure_connection_or_500()
    business_id = _resolve_current_business_id(db)
    query = {"business_id": business_id} if business_id else {"_id": None}
    discounts = [_serialize_discount(discount) for discount in db.discounts.find(query).sort("discount_name", 1)]

    return render_template(
        "services/manage_discounts.html",
        discounts=discounts,
        discount_categories=_build_filter_values(discounts, "discount_category"),
    )


@bp.route("/services/create", methods=["GET", "POST"])
def create_service():
    db = ensure_connection_or_500()
    business_id = _resolve_current_business_id(db)
    if not business_id:
        return redirect(url_for("admin_bp.admin"))

    error = ""
    form_data = _service_form_data()
    selected_part_category_ids_display = []
    selected_material_category_ids_display = []
    selected_equipment_category_ids_display = []
    business_doc = db.businesses.find_one({"_id": business_id}, {"labor_rate_standard": 1, "labor_rate_emergency": 1}) or {}
    default_labor_rate_standard = business_doc.get("labor_rate_standard")
    default_labor_rate_display = ""
    try:
        if default_labor_rate_standard is not None:
            default_labor_rate_display = f"{float(default_labor_rate_standard):.2f}"
    except (TypeError, ValueError):
        default_labor_rate_display = ""

    part_categories = _get_managed_categories(db, "part", business_id)
    material_categories = _get_managed_categories(db, "material", business_id)
    equipment_categories = _get_managed_categories(db, "equipment", business_id)

    if request.method == "POST":
        form_data = _service_form_data(request.form)
        tax_override = _parse_tax_override(request.form.get("tax_override"))
        selected_part_category_ids_display = _stringify_category_id_list(request.form.getlist("associated_part_category_ids"))
        selected_material_category_ids_display = _stringify_category_id_list(request.form.getlist("associated_material_category_ids"))
        selected_equipment_category_ids_display = _stringify_category_id_list(request.form.getlist("associated_equipment_category_ids"))

        selected_part_category_ids, error = _normalize_selected_category_ids(
            db, business_id, "part", selected_part_category_ids_display,
        )
        if not error:
            selected_material_category_ids, error = _normalize_selected_category_ids(
                db, business_id, "material", selected_material_category_ids_display,
            )
        if not error:
            selected_equipment_category_ids, error = _normalize_selected_category_ids(
                db, business_id, "equipment", selected_equipment_category_ids_display,
            )

        if not error:
            price, error = _parse_nonnegative_float(form_data["price"], "Price")
        if not error:
            labor_hours, error = _parse_optional_nonnegative_float(form_data["labor_hours"], "Labor Hours", default=None)
        if not error:
            labor_rate_override, error = _parse_optional_nonnegative_float(form_data["labor_rate_override"], "Labor Rate Override", default=None)

        if not error and not form_data["service_name"]:
            error = "Service Name is required."
        elif not error and not form_data["service_type"]:
            error = "Service Type is required."
        elif not error and form_data["service_type"] not in SERVICE_TYPE_OPTIONS:
            error = "Please select a valid Service Type."
        elif not error and labor_hours not in [None, 0] and (business_doc.get("labor_rate_standard") is None or business_doc.get("labor_rate_emergency") is None):
            error = "Set standard and emergency labor rates in Business Profile before creating services with labor hours."

        if not error:
            db.services.insert_one(
                {
                    "business_id": business_id,
                    "name": form_data["service_name"],
                    "service_name": form_data["service_name"],
                    "service_type": form_data["service_type"],
                    "description": form_data["description"],
                    "price": price,
                    "standard_price": price,
                    "labor_hours": labor_hours,
                    "estimated_hours": labor_hours,
                    "labor_rate_override": labor_rate_override,
                    "show_labor_breakdown": _parse_boolean(form_data.get("show_labor_breakdown")),
                    "tax_override": tax_override,
                    "associated_part_category_ids": selected_part_category_ids,
                    "associated_material_category_ids": selected_material_category_ids,
                    "associated_equipment_category_ids": selected_equipment_category_ids,
                    "is_active": True,
                }
            )
            return redirect(url_for("catalog.manage_services"))

    return render_template(
        "services/create_service.html",
        error=error,
        form_data=form_data,
        part_categories=part_categories,
        material_categories=material_categories,
        equipment_categories=equipment_categories,
        selected_part_category_ids=selected_part_category_ids_display,
        selected_material_category_ids=selected_material_category_ids_display,
        selected_equipment_category_ids=selected_equipment_category_ids_display,
        service_type_options=SERVICE_TYPE_OPTIONS,
        default_labor_rate_display=default_labor_rate_display,
    )


@bp.route("/services/<serviceId>")
def view_service(serviceId):
    db = ensure_connection_or_500()
    business_id = _resolve_current_business_id(db)
    query = {"_id": object_id_or_404(serviceId)}
    if business_id:
        query["business_id"] = business_id

    service = db.services.find_one(query)
    if not service:
        return redirect(url_for("catalog.manage_services"))

    service_category_ids = _service_associated_category_ids(service)
    selected_part_category_ids = {str(category_id) for category_id in service_category_ids["associated_part_category_ids"] if str(category_id).strip()}
    selected_material_category_ids = {str(category_id) for category_id in service_category_ids["associated_material_category_ids"] if str(category_id).strip()}
    selected_equipment_category_ids = {str(category_id) for category_id in service_category_ids["associated_equipment_category_ids"] if str(category_id).strip()}
    associated_part_categories = [category for category in _get_managed_categories(db, "part", business_id) if str(category.get("_id") or "") in selected_part_category_ids]
    associated_material_categories = [category for category in _get_managed_categories(db, "material", business_id) if str(category.get("_id") or "") in selected_material_category_ids]
    associated_equipment_categories = [category for category in _get_managed_categories(db, "equipment", business_id) if str(category.get("_id") or "") in selected_equipment_category_ids]

    return render_template(
        "services/view_service.html",
        serviceId=serviceId,
        service=_serialize_service(service),
        associated_part_categories=associated_part_categories,
        associated_material_categories=associated_material_categories,
        associated_equipment_categories=associated_equipment_categories,
    )


@bp.route("/services/<serviceId>/update", methods=["GET", "POST"])
def update_service(serviceId):
    db = ensure_connection_or_500()
    business_id = _resolve_current_business_id(db)
    query = {"_id": object_id_or_404(serviceId)}
    if business_id:
        query["business_id"] = business_id

    service = db.services.find_one(query)
    if not service:
        return redirect(url_for("catalog.manage_services"))

    error = ""
    form_data = _service_form_data(service)
    selected_category_ids = _service_associated_category_ids(service)
    selected_part_category_ids_display = selected_category_ids["associated_part_category_ids"]
    selected_material_category_ids_display = selected_category_ids["associated_material_category_ids"]
    selected_equipment_category_ids_display = selected_category_ids["associated_equipment_category_ids"]
    part_categories = _get_managed_categories(db, "part", business_id)
    material_categories = _get_managed_categories(db, "material", business_id)
    equipment_categories = _get_managed_categories(db, "equipment", business_id)
    business_doc = db.businesses.find_one({"_id": business_id}, {"labor_rate_standard": 1, "labor_rate_emergency": 1}) or {}

    if request.method == "POST":
        form_data = _service_form_data(request.form)
        selected_part_category_ids_display = _stringify_category_id_list(request.form.getlist("associated_part_category_ids"))
        selected_material_category_ids_display = _stringify_category_id_list(request.form.getlist("associated_material_category_ids"))
        selected_equipment_category_ids_display = _stringify_category_id_list(request.form.getlist("associated_equipment_category_ids"))

        if not error:
            price, error = _parse_nonnegative_float(form_data["price"], "Price")
        if not error:
            labor_hours, error = _parse_optional_nonnegative_float(form_data["labor_hours"], "Labor Hours", default=None)
        if not error:
            labor_rate_override, error = _parse_optional_nonnegative_float(form_data["labor_rate_override"], "Labor Rate Override", default=None)

        selected_part_category_ids = selected_category_ids["associated_part_category_ids"]
        selected_material_category_ids = selected_category_ids["associated_material_category_ids"]
        selected_equipment_category_ids = selected_category_ids["associated_equipment_category_ids"]
        if not error:
            selected_part_category_ids, error = _normalize_selected_category_ids(
                db, business_id, "part", selected_part_category_ids_display,
            )
        if not error:
            selected_material_category_ids, error = _normalize_selected_category_ids(
                db, business_id, "material", selected_material_category_ids_display,
            )
        if not error:
            selected_equipment_category_ids, error = _normalize_selected_category_ids(
                db, business_id, "equipment", selected_equipment_category_ids_display,
            )

        if not error and not form_data["service_name"]:
            error = "Service Name is required."
        elif not error and not form_data["service_type"]:
            error = "Service Type is required."
        elif not error and form_data["service_type"] not in SERVICE_TYPE_OPTIONS:
            error = "Please select a valid Service Type."
        elif not error and labor_hours not in [None, 0] and (business_doc.get("labor_rate_standard") is None or business_doc.get("labor_rate_emergency") is None):
            error = "Set standard and emergency labor rates in Business Profile before using services with labor hours."

        if not error:
            tax_override = _parse_tax_override(request.form.get("tax_override"))
            db.services.update_one(
                query,
                {
                    "$set": {
                        "name": form_data["service_name"],
                        "service_name": form_data["service_name"],
                        "service_type": form_data["service_type"],
                        "description": form_data["description"],
                        "price": price,
                        "standard_price": price,
                        "labor_hours": labor_hours,
                        "estimated_hours": labor_hours,
                        "labor_rate_override": labor_rate_override,
                        "show_labor_breakdown": _parse_boolean(form_data.get("show_labor_breakdown")),
                        "tax_override": tax_override,
                        "associated_part_category_ids": selected_part_category_ids,
                        "associated_material_category_ids": selected_material_category_ids,
                        "associated_equipment_category_ids": selected_equipment_category_ids,
                    },
                    "$unset": {
                        "service_code": "",
                        "included_parts": "",
                        "part_ids": "",
                        "service_parts": "",
                        "material_ids": "",
                        "service_materials": "",
                    },
                },
            )
            return redirect(url_for("catalog.view_service", serviceId=serviceId))

    return render_template(
        "services/update_service.html",
        serviceId=serviceId,
        error=error,
        form_data=form_data,
        part_categories=part_categories,
        material_categories=material_categories,
        equipment_categories=equipment_categories,
        selected_part_category_ids=selected_part_category_ids_display,
        selected_material_category_ids=selected_material_category_ids_display,
        selected_equipment_category_ids=selected_equipment_category_ids_display,
        service_type_options=SERVICE_TYPE_OPTIONS,
    )


@bp.route("/services/<serviceId>/delete", methods=["POST"])
def delete_service(serviceId):
    db = ensure_connection_or_500()
    business_id = _resolve_current_business_id(db)
    query = {"_id": object_id_or_404(serviceId)}
    if business_id:
        query["business_id"] = business_id
    db.services.delete_one(query)
    return redirect(url_for("catalog.manage_services"))


@bp.route("/parts/create", methods=["GET", "POST"])
def create_part():
    db = ensure_connection_or_500()
    business_id = _resolve_current_business_id(db)
    if not business_id:
        return redirect(url_for("admin_bp.admin"))

    error = ""
    form_data = _part_form_data()
    category_options = _build_managed_category_options(db, "part", business_id)
    markup_rules = _get_business_markup_rules(db, business_id)

    if request.method == "POST":
        form_data = _part_form_data(request.form)
        tax_override = _parse_tax_override(request.form.get("tax_override"))
        sell_price_auto_populated = _parse_boolean(request.form.get("sell_price_auto_populated"))
        cost_price, error = _parse_nonnegative_float(form_data["cost_price"], "Cost Price")
        if not error:
            sell_price, error = _parse_nonnegative_float(form_data["sell_price"], "Sell Price")
        if not error:
            warranty_months, error = _parse_optional_integer(form_data["warranty_months"], "Warranty Months")

        if not error and not form_data["part_name"]:
            error = "Part Name is required."
        elif not error and not form_data["category"]:
            error = "Category is required."
        elif not error and not _category_exists(db, "part", business_id, form_data["category"]):
            error = "Please select a valid category."

        if not error:
            db.parts.insert_one(
                {
                    "business_id": business_id,
                    "name": form_data["part_name"],
                    "part_name": form_data["part_name"],
                    "category": form_data["category"],
                    "sku": form_data["sku"],
                    "description": form_data["description"],
                    "cost_price": cost_price,
                    "sell_price": sell_price,
                    "unit_cost": sell_price,
                    "warranty_months": warranty_months,
                    "purchase_link": form_data["purchase_link"],
                    "sell_price_auto_populated": sell_price_auto_populated,
                    "tax_override": tax_override,
                    "is_active": True,
                }
            )
            return redirect(url_for("catalog.manage_parts"))

    return render_template(
        "services/create_part.html",
        error=error,
        form_data=form_data,
        category_options=category_options,
        markup_rules=markup_rules,
    )


@bp.route("/parts/<partId>")
def view_part(partId):
    db = ensure_connection_or_500()
    business_id = _resolve_current_business_id(db)
    query = {"_id": object_id_or_404(partId)}
    if business_id:
        query["business_id"] = business_id
    part = db.parts.find_one(query)
    if not part:
        return redirect(url_for("catalog.manage_parts"))

    return render_template("services/view_part.html", partId=partId, part=_serialize_part(part))


@bp.route("/parts/<partId>/update", methods=["GET", "POST"])
def update_part(partId):
    db = ensure_connection_or_500()
    business_id = _resolve_current_business_id(db)
    query = {"_id": object_id_or_404(partId)}
    if business_id:
        query["business_id"] = business_id
    part = db.parts.find_one(query)
    if not part:
        return redirect(url_for("catalog.manage_parts"))

    error = ""
    form_data = _part_form_data(part)
    category_options = _build_managed_category_options(db, "part", business_id)
    markup_rules = _get_business_markup_rules(db, business_id)

    if request.method == "POST":
        form_data = _part_form_data(request.form)
        tax_override = _parse_tax_override(request.form.get("tax_override"))
        sell_price_auto_populated = _parse_boolean(request.form.get("sell_price_auto_populated"))
        cost_price, error = _parse_nonnegative_float(form_data["cost_price"], "Cost Price")
        if not error:
            sell_price, error = _parse_nonnegative_float(form_data["sell_price"], "Sell Price")
        if not error:
            warranty_months, error = _parse_optional_integer(form_data["warranty_months"], "Warranty Months")

        if not error and not form_data["part_name"]:
            error = "Part Name is required."
        elif not error and not form_data["category"]:
            error = "Category is required."
        elif not error and not _category_exists(db, "part", business_id, form_data["category"]):
            error = "Please select a valid category."

        if not error:
            db.parts.update_one(
                query,
                {"$set": {
                    "name": form_data["part_name"],
                    "part_name": form_data["part_name"],
                    "category": form_data["category"],
                    "sku": form_data["sku"],
                    "description": form_data["description"],
                    "cost_price": cost_price,
                    "sell_price": sell_price,
                    "unit_cost": sell_price,
                    "warranty_months": warranty_months,
                    "purchase_link": form_data["purchase_link"],
                    "sell_price_auto_populated": sell_price_auto_populated,
                    "tax_override": tax_override,
                }, "$unset": {"part_code": "", "manufacturer": "", "model_number": ""}},
            )
            return redirect(url_for("catalog.view_part", partId=partId))

    return render_template(
        "services/update_part.html",
        partId=partId,
        error=error,
        form_data=form_data,
        category_options=category_options,
        markup_rules=markup_rules,
    )


@bp.route("/parts/<partId>/delete", methods=["POST"])
def delete_part(partId):
    db = ensure_connection_or_500()
    business_id = _resolve_current_business_id(db)
    query = {"_id": object_id_or_404(partId)}
    if business_id:
        query["business_id"] = business_id
    db.parts.delete_one(query)
    return redirect(url_for("catalog.manage_parts"))


@bp.route("/labor/create", methods=["GET", "POST"])
def create_labor():
    db = ensure_connection_or_500()
    business_id = _resolve_current_business_id(db)
    if not business_id:
        return redirect(url_for("admin_bp.admin"))

    error = ""
    form_data = _labor_form_data()
    category_options = _build_category_options(db, "labors", "labor_category", business_id)

    if request.method == "POST":
        form_data = _labor_form_data(request.form)
        tax_override = _parse_tax_override(request.form.get("tax_override"))
        labor_default_hours, error = _parse_nonnegative_float(form_data["labor_default_hours"], "Default Hours")
        if not error:
            labor_hourly_rate, error = _parse_nonnegative_float(form_data["labor_hourly_rate"], "Hourly Rate")

        if not error and not form_data["labor_description"]:
            error = "Labor Description is required."
        elif not error and not form_data["labor_category"]:
            error = "Labor Category is required."

        if not error:
            db.labors.insert_one(
                {
                    "business_id": business_id,
                    "labor_description": form_data["labor_description"],
                    "labor_category": form_data["labor_category"],
                    "labor_default_hours": labor_default_hours,
                    "labor_hourly_rate": labor_hourly_rate,
                    "tax_override": tax_override,
                }
            )
            return redirect(url_for("catalog.manage_labor"))

    return render_template("services/create_labor.html", error=error, form_data=form_data, category_options=category_options)


@bp.route("/labor/<laborId>")
def view_labor(laborId):
    db = ensure_connection_or_500()
    business_id = _resolve_current_business_id(db)
    query = {"_id": object_id_or_404(laborId)}
    if business_id:
        query["business_id"] = business_id
    labor = db.labors.find_one(query)
    if not labor:
        return redirect(url_for("catalog.manage_labor"))

    return render_template("services/view_labor.html", laborId=laborId, labor=_serialize_labor(labor))


@bp.route("/labor/<laborId>/update", methods=["GET", "POST"])
def update_labor(laborId):
    db = ensure_connection_or_500()
    business_id = _resolve_current_business_id(db)
    query = {"_id": object_id_or_404(laborId)}
    if business_id:
        query["business_id"] = business_id
    labor = db.labors.find_one(query)
    if not labor:
        return redirect(url_for("catalog.manage_labor"))

    error = ""
    form_data = _labor_form_data(labor)
    category_options = _build_category_options(db, "labors", "labor_category", business_id)

    if request.method == "POST":
        form_data = _labor_form_data(request.form)
        tax_override = _parse_tax_override(request.form.get("tax_override"))
        labor_default_hours, error = _parse_nonnegative_float(form_data["labor_default_hours"], "Default Hours")
        if not error:
            labor_hourly_rate, error = _parse_nonnegative_float(form_data["labor_hourly_rate"], "Hourly Rate")

        if not error and not form_data["labor_description"]:
            error = "Labor Description is required."
        elif not error and not form_data["labor_category"]:
            error = "Labor Category is required."

        if not error:
            db.labors.update_one(
                query,
                {
                    "$set": {
                        "labor_description": form_data["labor_description"],
                        "labor_category": form_data["labor_category"],
                        "labor_default_hours": labor_default_hours,
                        "labor_hourly_rate": labor_hourly_rate,
                        "tax_override": tax_override,
                    }
                },
            )
            return redirect(url_for("catalog.view_labor", laborId=laborId))

    return render_template("services/update_labor.html", laborId=laborId, error=error, form_data=form_data, category_options=category_options)


@bp.route("/labor/<laborId>/delete", methods=["POST"])
def delete_labor(laborId):
    db = ensure_connection_or_500()
    business_id = _resolve_current_business_id(db)
    query = {"_id": object_id_or_404(laborId)}
    if business_id:
        query["business_id"] = business_id
    db.labors.delete_one(query)
    return redirect(url_for("catalog.manage_labor"))


@bp.route("/materials/create", methods=["GET", "POST"])
def create_material():
    db = ensure_connection_or_500()
    business_id = _resolve_current_business_id(db)
    if not business_id:
        return redirect(url_for("admin_bp.admin"))

    error = ""
    form_data = _material_form_data()
    uom_options = _material_uom_options()
    category_options = _build_managed_category_options(db, "material", business_id)
    markup_rules = _get_business_markup_rules(db, business_id)

    if request.method == "POST":
        form_data = _material_form_data(request.form)
        tax_override = _parse_tax_override(request.form.get("tax_override"))
        sell_price_auto_populated = _parse_boolean(request.form.get("sell_price_auto_populated"))
        minimum_quantity, error = _parse_optional_nonnegative_float(form_data["minimum_quantity"], "Minimum Quantity", default=None)
        if not error:
            cost_price_per_unit, error = _parse_nonnegative_float(form_data["cost_price_per_unit"], "Cost Price Per Unit")
        if not error:
            sell_price_per_unit, error = _parse_nonnegative_float(form_data["sell_price_per_unit"], "Sell Price Per Unit")

        if not error and not form_data["material_name"]:
            error = "Material Name is required."
        elif not error and not form_data["unit_of_measure"]:
            error = "Unit of Measure is required."
        elif not error and form_data["unit_of_measure"] not in MATERIAL_UOM_OPTIONS:
            error = "Please select a valid Unit of Measure."
        elif not error and not form_data["unit_label"]:
            error = "Unit Label is required."
        elif not error and not _category_exists(db, "material", business_id, form_data["category"]):
            error = "Please select a valid category."

        if not error:
            db.materials.insert_one(
                {
                    "business_id": business_id,
                    "material_name": form_data["material_name"],
                    "description": form_data["description"],
                    "category": form_data["category"],
                    "unit_of_measure": form_data["unit_of_measure"],
                    "unit_label": form_data["unit_label"],
                    "cost_price_per_unit": cost_price_per_unit,
                    "sell_price_per_unit": sell_price_per_unit,
                    "minimum_quantity": minimum_quantity,
                    "default_quantity_used": minimum_quantity,
                    "price": sell_price_per_unit,
                    "is_active": True,
                    "sort_order": 0,
                    "purchase_link": form_data["purchase_link"],
                    "sell_price_auto_populated": sell_price_auto_populated,
                    "tax_override": tax_override,
                }
            )
            return redirect(url_for("catalog.manage_materials"))

    return render_template(
        "services/create_materials.html",
        error=error,
        form_data=form_data,
        uom_options=uom_options,
        category_options=category_options,
        markup_rules=markup_rules,
    )


@bp.route("/materials/<materialId>")
def view_material(materialId):
    db = ensure_connection_or_500()
    business_id = _resolve_current_business_id(db)
    query = {"_id": object_id_or_404(materialId)}
    if business_id:
        query["business_id"] = business_id
    material = db.materials.find_one(query)
    if not material:
        return redirect(url_for("catalog.manage_materials"))

    return render_template("services/view_materials.html", materialId=materialId, material=_serialize_material(material))


@bp.route("/materials/<materialId>/update", methods=["GET", "POST"])
def update_material(materialId):
    db = ensure_connection_or_500()
    business_id = _resolve_current_business_id(db)
    query = {"_id": object_id_or_404(materialId)}
    if business_id:
        query["business_id"] = business_id
    material = db.materials.find_one(query)
    if not material:
        return redirect(url_for("catalog.manage_materials"))

    error = ""
    form_data = _material_form_data(material)
    uom_options = _material_uom_options()
    category_options = _build_managed_category_options(db, "material", business_id)
    markup_rules = _get_business_markup_rules(db, business_id)

    if request.method == "POST":
        form_data = _material_form_data(request.form)
        tax_override = _parse_tax_override(request.form.get("tax_override"))
        sell_price_auto_populated = _parse_boolean(request.form.get("sell_price_auto_populated"))
        minimum_quantity, error = _parse_optional_nonnegative_float(form_data["minimum_quantity"], "Minimum Quantity", default=None)
        if not error:
            cost_price_per_unit, error = _parse_nonnegative_float(form_data["cost_price_per_unit"], "Cost Price Per Unit")
        if not error:
            sell_price_per_unit, error = _parse_nonnegative_float(form_data["sell_price_per_unit"], "Sell Price Per Unit")
        if not error:
            sort_order, error = _parse_optional_integer(form_data["sort_order"], "Sort Order")

        if not error and not form_data["material_name"]:
            error = "Material Name is required."
        elif not error and not form_data["unit_of_measure"]:
            error = "Unit of Measure is required."
        elif not error and form_data["unit_of_measure"] not in MATERIAL_UOM_OPTIONS:
            error = "Please select a valid Unit of Measure."
        elif not error and not form_data["unit_label"]:
            error = "Unit Label is required."
        elif not error and not _category_exists(db, "material", business_id, form_data["category"]):
            error = "Please select a valid category."

        if not error:
            db.materials.update_one(
                query,
                {
                    "$set": {
                        "material_name": form_data["material_name"],
                        "description": form_data["description"],
                        "category": form_data["category"],
                        "part_number": form_data["part_number"],
                        "manufacturer": form_data["manufacturer"],
                        "unit_of_measure": form_data["unit_of_measure"],
                        "unit_label": form_data["unit_label"],
                        "cost_price_per_unit": cost_price_per_unit,
                        "sell_price_per_unit": sell_price_per_unit,
                        "minimum_quantity": minimum_quantity,
                        "default_quantity_used": minimum_quantity,
                        "price": sell_price_per_unit,
                        "is_active": _parse_boolean(form_data.get("is_active")),
                        "sort_order": sort_order if sort_order is not None else 0,
                        "purchase_link": form_data["purchase_link"],
                        "sell_price_auto_populated": sell_price_auto_populated,
                        "tax_override": tax_override,
                    }
                },
            )
            return redirect(url_for("catalog.view_material", materialId=materialId))

    return render_template(
        "services/update_materials.html",
        materialId=materialId,
        error=error,
        form_data=form_data,
        uom_options=uom_options,
        category_options=category_options,
        markup_rules=markup_rules,
    )


@bp.route("/materials/<materialId>/delete", methods=["POST"])
def delete_material(materialId):
    db = ensure_connection_or_500()
    business_id = _resolve_current_business_id(db)
    query = {"_id": object_id_or_404(materialId)}
    if business_id:
        query["business_id"] = business_id
    db.materials.delete_one(query)
    return redirect(url_for("catalog.manage_materials"))


@bp.route("/equipment/create", methods=["GET", "POST"])
def create_equipment():
    db = ensure_connection_or_500()
    business_id = _resolve_current_business_id(db)
    if not business_id:
        return redirect(url_for("admin_bp.admin"))

    error = ""
    form_data = _equipment_form_data()
    category_options = _build_managed_category_options(db, "equipment", business_id)

    if request.method == "POST":
        form_data = _equipment_form_data(request.form)
        tax_override = _parse_tax_override(request.form.get("tax_override"))
        cost_price, error = _parse_nonnegative_float(form_data["cost_price"], "Cost Price")
        if not error:
            sell_price, error = _parse_nonnegative_float(form_data["sell_price"], "Sell Price")
        if not error:
            warranty_months, error = _parse_optional_integer(form_data["warranty_months"], "Warranty Months")
        if not error:
            sort_order, error = _parse_optional_integer(form_data["sort_order"], "Sort Order")
        if not error:
            seer_rating, error = _parse_optional_nonnegative_float(form_data["seer_rating"], "SEER Rating", default=None)
        if not error:
            afue_rating, error = _parse_optional_nonnegative_float(form_data["afue_rating"], "AFUE Rating", default=None)

        if not error and not form_data["equipment_name"]:
            error = "Equipment Name is required."
        elif not error and not form_data["equipment_type"]:
            error = "Equipment Type is required."
        elif not error and form_data["equipment_type"] not in EQUIPMENT_TYPE_OPTIONS:
            error = "Please select a valid Equipment Type."
        elif not error and not form_data["category"]:
            error = "Category is required."
        elif not error and not _category_exists(db, "equipment", business_id, form_data["category"]):
            error = "Please select a valid category."

        if not error:
            db.equipment.insert_one(
                {
                    "business_id": business_id,
                    "equipment_name": form_data["equipment_name"],
                    "equipment_type": form_data["equipment_type"],
                    "manufacturer": form_data["manufacturer"],
                    "category": form_data["category"],
                    "sku": form_data["sku"],
                    "model_number": form_data["model_number"],
                    "description": form_data["description"],
                    "purchase_link": form_data["purchase_link"],
                    "cost_price": cost_price,
                    "sell_price": sell_price,
                    "warranty_months": warranty_months,
                    "is_active": _parse_boolean(form_data.get("is_active")),
                    "sort_order": sort_order if sort_order is not None else 0,
                    "cooling_capacity": form_data["cooling_capacity"],
                    "seer_rating": seer_rating,
                    "metering_device": form_data["metering_device"],
                    "afue_rating": afue_rating,
                    "btu_input": form_data["btu_input"],
                    "btu_output": form_data["btu_output"],
                    "refrigerant_type": form_data["refrigerant_type"],
                    "stages": form_data["stages"],
                    "blower_motor_type": form_data["blower_motor_type"],
                    "voltage": form_data["voltage"],
                    "tax_override": tax_override,
                }
            )
            return redirect(url_for("catalog.manage_equipment"))

    return render_template(
        "services/create_equipment.html",
        error=error,
        form_data=form_data,
        category_options=category_options,
        equipment_type_options=EQUIPMENT_TYPE_OPTIONS,
        equipment_cooling_capacity_options=EQUIPMENT_COOLING_CAPACITY_OPTIONS,
        equipment_refrigerant_type_options=EQUIPMENT_REFRIGERANT_TYPE_OPTIONS,
        equipment_btu_options=EQUIPMENT_BTU_OPTIONS,
        equipment_stages_options=EQUIPMENT_STAGES_OPTIONS,
        equipment_blower_motor_type_options=EQUIPMENT_BLOWER_MOTOR_TYPE_OPTIONS,
    )


@bp.route("/equipment/<equipmentId>")
def view_equipment(equipmentId):
    db = ensure_connection_or_500()
    business_id = _resolve_current_business_id(db)
    query = {"_id": object_id_or_404(equipmentId)}
    if business_id:
        query["business_id"] = business_id
    equipment = db.equipment.find_one(query)
    if not equipment:
        return redirect(url_for("catalog.manage_equipment"))

    return render_template("services/view_equipment.html", equipmentId=equipmentId, equipment=_serialize_equipment(equipment))


@bp.route("/equipment/<equipmentId>/update", methods=["GET", "POST"])
def update_equipment(equipmentId):
    db = ensure_connection_or_500()
    business_id = _resolve_current_business_id(db)
    query = {"_id": object_id_or_404(equipmentId)}
    if business_id:
        query["business_id"] = business_id
    equipment = db.equipment.find_one(query)
    if not equipment:
        return redirect(url_for("catalog.manage_equipment"))

    error = ""
    form_data = _equipment_form_data(equipment)
    category_options = _build_managed_category_options(db, "equipment", business_id)

    if request.method == "POST":
        form_data = _equipment_form_data(request.form)
        tax_override = _parse_tax_override(request.form.get("tax_override"))
        cost_price, error = _parse_nonnegative_float(form_data["cost_price"], "Cost Price")
        if not error:
            sell_price, error = _parse_nonnegative_float(form_data["sell_price"], "Sell Price")
        if not error:
            warranty_months, error = _parse_optional_integer(form_data["warranty_months"], "Warranty Months")
        if not error:
            sort_order, error = _parse_optional_integer(form_data["sort_order"], "Sort Order")
        if not error:
            seer_rating, error = _parse_optional_nonnegative_float(form_data["seer_rating"], "SEER Rating", default=None)
        if not error:
            afue_rating, error = _parse_optional_nonnegative_float(form_data["afue_rating"], "AFUE Rating", default=None)

        if not error and not form_data["equipment_name"]:
            error = "Equipment Name is required."
        elif not error and not form_data["equipment_type"]:
            error = "Equipment Type is required."
        elif not error and form_data["equipment_type"] not in EQUIPMENT_TYPE_OPTIONS:
            error = "Please select a valid Equipment Type."
        elif not error and not form_data["category"]:
            error = "Category is required."
        elif not error and not _category_exists(db, "equipment", business_id, form_data["category"]):
            error = "Please select a valid category."

        if not error:
            db.equipment.update_one(
                query,
                {
                    "$set": {
                        "equipment_name": form_data["equipment_name"],
                        "equipment_type": form_data["equipment_type"],
                        "manufacturer": form_data["manufacturer"],
                        "category": form_data["category"],
                        "sku": form_data["sku"],
                        "model_number": form_data["model_number"],
                        "description": form_data["description"],
                        "purchase_link": form_data["purchase_link"],
                        "cost_price": cost_price,
                        "sell_price": sell_price,
                        "warranty_months": warranty_months,
                        "is_active": _parse_boolean(form_data.get("is_active")),
                        "sort_order": sort_order if sort_order is not None else 0,
                        "cooling_capacity": form_data["cooling_capacity"],
                        "seer_rating": seer_rating,
                        "metering_device": form_data["metering_device"],
                        "afue_rating": afue_rating,
                        "btu_input": form_data["btu_input"],
                        "btu_output": form_data["btu_output"],
                        "refrigerant_type": form_data["refrigerant_type"],
                        "stages": form_data["stages"],
                        "blower_motor_type": form_data["blower_motor_type"],
                        "voltage": form_data["voltage"],
                        "tax_override": tax_override,
                    }
                },
            )
            return redirect(url_for("catalog.view_equipment", equipmentId=equipmentId))

    return render_template(
        "services/update_equipment.html",
        equipmentId=equipmentId,
        error=error,
        form_data=form_data,
        category_options=category_options,
        equipment_type_options=EQUIPMENT_TYPE_OPTIONS,
        equipment_cooling_capacity_options=EQUIPMENT_COOLING_CAPACITY_OPTIONS,
        equipment_refrigerant_type_options=EQUIPMENT_REFRIGERANT_TYPE_OPTIONS,
        equipment_btu_options=EQUIPMENT_BTU_OPTIONS,
        equipment_stages_options=EQUIPMENT_STAGES_OPTIONS,
        equipment_blower_motor_type_options=EQUIPMENT_BLOWER_MOTOR_TYPE_OPTIONS,
    )


@bp.route("/equipment/<equipmentId>/delete", methods=["POST"])
def delete_equipment(equipmentId):
    db = ensure_connection_or_500()
    business_id = _resolve_current_business_id(db)
    query = {"_id": object_id_or_404(equipmentId)}
    if business_id:
        query["business_id"] = business_id
    db.equipment.delete_one(query)
    return redirect(url_for("catalog.manage_equipment"))


@bp.route("/discounts/create", methods=["GET", "POST"])
def create_discount():
    db = ensure_connection_or_500()
    business_id = _resolve_current_business_id(db)
    if not business_id:
        return redirect(url_for("admin_bp.admin"))

    error = ""
    form_data = _discount_form_data()
    category_options = _build_category_options(db, "discounts", "discount_category", business_id)

    if request.method == "POST":
        form_data = _discount_form_data(request.form)
        discount_percentage, error = _parse_optional_nonnegative_float(form_data["discount_percentage"], "Discount Percentage", default=None)
        if not error:
            discount_amount, error = _parse_optional_nonnegative_float(form_data["discount_amount"], "Discount Amount", default=None)

        has_percentage = discount_percentage is not None
        has_amount = discount_amount is not None
        if not error and has_percentage == has_amount:
            error = "Enter either Discount Percentage or Discount Amount, but not both."

        if not error and has_percentage and discount_percentage > 100:
            error = "Discount Percentage cannot exceed 100."

        if not error and not form_data["discount_name"]:
            error = "Discount Name is required."
        elif not error and not form_data["discount_category"]:
            error = "Discount Category is required."

        if not error:
            db.discounts.insert_one(
                {
                    "business_id": business_id,
                    "discount_name": form_data["discount_name"],
                    "discount_category": form_data["discount_category"],
                    "discount_percentage": discount_percentage,
                    "discount_amount": discount_amount,
                }
            )
            return redirect(url_for("catalog.manage_discounts"))

    return render_template("services/create_discount.html", error=error, form_data=form_data, category_options=category_options)


@bp.route("/discounts/<discountId>")
def view_discount(discountId):
    db = ensure_connection_or_500()
    business_id = _resolve_current_business_id(db)
    query = {"_id": object_id_or_404(discountId)}
    if business_id:
        query["business_id"] = business_id
    discount = db.discounts.find_one(query)
    if not discount:
        return redirect(url_for("catalog.manage_discounts"))

    return render_template("services/view_discount.html", discountId=discountId, discount=_serialize_discount(discount))


@bp.route("/discounts/<discountId>/update", methods=["GET", "POST"])
def update_discount(discountId):
    db = ensure_connection_or_500()
    business_id = _resolve_current_business_id(db)
    query = {"_id": object_id_or_404(discountId)}
    if business_id:
        query["business_id"] = business_id
    discount = db.discounts.find_one(query)
    if not discount:
        return redirect(url_for("catalog.manage_discounts"))

    error = ""
    form_data = _discount_form_data(discount)
    category_options = _build_category_options(db, "discounts", "discount_category", business_id)

    if request.method == "POST":
        form_data = _discount_form_data(request.form)
        discount_percentage, error = _parse_optional_nonnegative_float(form_data["discount_percentage"], "Discount Percentage", default=None)
        if not error:
            discount_amount, error = _parse_optional_nonnegative_float(form_data["discount_amount"], "Discount Amount", default=None)

        has_percentage = discount_percentage is not None
        has_amount = discount_amount is not None
        if not error and has_percentage == has_amount:
            error = "Enter either Discount Percentage or Discount Amount, but not both."

        if not error and has_percentage and discount_percentage > 100:
            error = "Discount Percentage cannot exceed 100."

        if not error and not form_data["discount_name"]:
            error = "Discount Name is required."
        elif not error and not form_data["discount_category"]:
            error = "Discount Category is required."

        if not error:
            db.discounts.update_one(
                query,
                {
                    "$set": {
                        "discount_name": form_data["discount_name"],
                        "discount_category": form_data["discount_category"],
                        "discount_percentage": discount_percentage,
                        "discount_amount": discount_amount,
                    }
                },
            )
            return redirect(url_for("catalog.view_discount", discountId=discountId))

    return render_template("services/update_discount.html", discountId=discountId, error=error, form_data=form_data, category_options=category_options)


@bp.route("/discounts/<discountId>/delete", methods=["POST"])
def delete_discount(discountId):
    db = ensure_connection_or_500()
    business_id = _resolve_current_business_id(db)
    query = {"_id": object_id_or_404(discountId)}
    if business_id:
        query["business_id"] = business_id
    db.discounts.delete_one(query)
    return redirect(url_for("catalog.manage_discounts"))

