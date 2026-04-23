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

EQUIPMENT_TYPE_OPTIONS = [
    "Central Air Conditioner (Split)",
    "Mini-Split",
    "Heat Pump",
    "Portable / Window Unit",
    "Chiller",
    "Air Handler",
    "Gas Furnace",
    "Electric Furnace",
    "Boiler",
    "Radiant Heating",
    "Space Heater / Unit Heater",
    "Thermostat (Smart)",
    "Thermostat (Programmable)",
    "Zoning System / Controllers",
    "Building Automation System",
    "Heat Recovery Ventillator",
    "Energy Recovery Ventillator",
    "Rooftop Unit",
    "Whole Home Humidifier",
    "Whole Home Dehumidifier",
    "Air Cleaner",
    "Other",
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


def _parse_boolean(raw_value):
    return str(raw_value or "").strip().lower() in {"true", "1", "yes", "on"}


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


def _serialize_service(service):
    serialized = serialize_doc(service)
    serialized["service_type"] = str(serialized.get("service_type") or "").strip()
    serialized["category"] = str(serialized.get("category") or "").strip()
    serialized["emergency"] = _parse_boolean(serialized.get("emergency"))
    serialized["emergency_display"] = "Yes" if serialized["emergency"] else "No"
    serialized["emergency_price_display"] = _format_currency_display(serialized.get("emergency_price"))
    serialized["materials_cost_display"] = _format_currency_display(serialized.get("materials_cost"))
    serialized["standard_price_display"] = _format_currency_display(serialized.get("standard_price"))
    serialized["estimated_hours_display"] = _format_hours_display(serialized.get("estimated_hours"))
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
            rows.append(
                {
                    "material_id": str(entry.get("material_id") or "").strip(),
                    "default_quantity_used": _format_hours_display(entry.get("default_quantity_used")) if entry.get("default_quantity_used") is not None else "",
                    "unit_of_measure": str(entry.get("unit_of_measure") or "").strip(),
                    "price": _format_currency_display(entry.get("price")),
                }
            )
        return rows

    for index, material_id in enumerate(selected_material_ids):
        rows.append(
            {
                "material_id": str(material_id or "").strip(),
                "default_quantity_used": str(entered_quantities[index] or "").strip() if index < len(entered_quantities) else "",
                "unit_of_measure": str(entered_units[index] or "").strip() if index < len(entered_units) else "",
                "price": str(entered_prices[index] or "").strip() if index < len(entered_prices) else "",
            }
        )

    return rows or [{"material_id": "", "default_quantity_used": "", "unit_of_measure": "", "price": ""}]


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

        default_quantity_used, error = _parse_optional_nonnegative_float(raw_quantity, "Material Default Quantity Used", default=None)
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
                "default_quantity_used": default_quantity_used,
                "unit_of_measure": str(raw_unit or "").strip(),
                "price": price,
            }
        )

    return entries, seen_material_ids, ""


def _serialize_part(part):
    serialized = serialize_doc(part)
    serialized["category"] = str(serialized.get("category") or "").strip()
    serialized["manufacturer"] = str(serialized.get("manufacturer") or "").strip()
    serialized["unit_cost_display"] = _format_currency_display(serialized.get("unit_cost"))
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
    serialized["category"] = str(serialized.get("category") or "").strip()
    serialized["part_number"] = str(serialized.get("part_number") or "").strip()
    serialized["manufacturer"] = str(serialized.get("manufacturer") or "").strip()
    default_quantity = serialized.get("default_quantity_used")
    serialized["default_quantity_used"] = str(default_quantity or "").strip()
    serialized["default_quantity_used_display"] = _format_hours_display(default_quantity)
    serialized["unit_of_measure"] = str(serialized.get("unit_of_measure") or "").strip()
    serialized["price_display"] = _format_currency_display(serialized.get("price"))
    serialized["purchase_link"] = str(serialized.get("purchase_link") or "").strip()
    return serialized


def _serialize_equipment(equipment):
    serialized = serialize_doc(equipment)
    serialized["equipment_name"] = str(serialized.get("equipment_name") or "").strip()
    serialized["equipment_type"] = str(serialized.get("equipment_type") or "").strip()
    serialized["manufacturer"] = str(serialized.get("manufacturer") or "").strip()
    serialized["category"] = str(serialized.get("category") or "").strip()
    serialized["sku"] = str(serialized.get("sku") or "").strip()
    serialized["description"] = str(serialized.get("description") or "").strip()
    serialized["notes"] = str(serialized.get("notes") or "").strip()
    serialized["purchase_link"] = str(serialized.get("purchase_link") or "").strip()
    default_quantity = serialized.get("default_quantity_installed")
    serialized["default_quantity_installed"] = str(default_quantity or "").strip()
    serialized["default_quantity_installed_display"] = _format_hours_display(default_quantity)
    serialized["default_price_display"] = _format_currency_display(serialized.get("default_price"))
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


def _service_form_data(service=None):
    service = service or {}

    raw_emergency = service.get("emergency")
    if isinstance(raw_emergency, bool):
        emergency_value = "true" if raw_emergency else "false"
    else:
        emergency_value = "true" if _parse_boolean(raw_emergency) else "false"

    return {
        "service_name": str(service.get("service_name") or "").strip(),
        "service_type": str(service.get("service_type") or "").strip(),
        "service_code": str(service.get("service_code") or "").strip(),
        "category": str(service.get("category") or "").strip(),
        "description": str(service.get("description") or "").strip(),
        "materials_cost": str(service.get("materials_cost") or "").strip(),
        "estimated_hours": str(service.get("estimated_hours") or "").strip(),
        "standard_price": str(service.get("standard_price") or "").strip(),
        "emergency": emergency_value,
        "emergency_price": str(service.get("emergency_price") or "").strip(),
    }


def _part_form_data(part=None):
    part = part or {}
    return {
        "part_name": str(part.get("part_name") or "").strip(),
        "part_code": str(part.get("part_code") or "").strip(),
        "category": str(part.get("category") or "").strip(),
        "manufacturer": str(part.get("manufacturer") or "").strip(),
        "description": str(part.get("description") or "").strip(),
        "unit_cost": str(part.get("unit_cost") or "").strip(),
        "purchase_link": str(part.get("purchase_link") or "").strip(),
    }


def _labor_form_data(labor=None):
    labor = labor or {}
    return {
        "labor_description": str(labor.get("labor_description") or "").strip(),
        "labor_category": str(labor.get("labor_category") or "").strip(),
        "labor_default_hours": str(labor.get("labor_default_hours") or "").strip(),
        "labor_hourly_rate": str(labor.get("labor_hourly_rate") or "").strip(),
    }


def _material_form_data(material=None):
    material = material or {}
    return {
        "material_name": str(material.get("material_name") or "").strip(),
        "category": str(material.get("category") or "").strip(),
        "part_number": str(material.get("part_number") or "").strip(),
        "manufacturer": str(material.get("manufacturer") or "").strip(),
        "default_quantity_used": str(material.get("default_quantity_used") or "").strip(),
        "unit_of_measure": str(material.get("unit_of_measure") or "").strip(),
        "price": str(material.get("price") or "").strip(),
        "purchase_link": str(material.get("purchase_link") or "").strip(),
    }


def _equipment_form_data(equipment=None):
    equipment = equipment or {}
    return {
        "equipment_name": str(equipment.get("equipment_name") or "").strip(),
        "equipment_type": str(equipment.get("equipment_type") or "").strip(),
        "manufacturer": str(equipment.get("manufacturer") or "").strip(),
        "category": str(equipment.get("category") or "").strip(),
        "sku": str(equipment.get("sku") or "").strip(),
        "description": str(equipment.get("description") or "").strip(),
        "notes": str(equipment.get("notes") or "").strip(),
        "purchase_link": str(equipment.get("purchase_link") or "").strip(),
        "default_price": str(equipment.get("default_price") or "").strip(),
        "default_quantity_installed": str(equipment.get("default_quantity_installed") or "").strip(),
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
    return ["ea", "ft", "in", "lb", "oz", "gal", "qt", "pt", "L", "mL", "sq ft", "cu ft", "box", "roll", "set"]


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
    services = [_serialize_service(service) for service in db.services.find(query).sort("service_name", 1)]

    return render_template(
        "services/manage_services.html",
        services=services,
        service_categories=_build_filter_values(services, "category"),
        service_codes=_build_filter_values(services, "service_code"),
    )


@bp.route("/services/export/csv")
def export_services_csv():
    db = ensure_connection_or_500()
    business_id = _resolve_current_business_id(db)
    query = {"business_id": business_id} if business_id else {"_id": None}
    rows = list(db.services.find(query).sort("service_name", 1))
    return build_csv_export_response(rows, "services_export.csv")


@bp.route("/parts")
def manage_parts():
    db = ensure_connection_or_500()
    business_id = _resolve_current_business_id(db)
    query = {"business_id": business_id} if business_id else {"_id": None}
    parts = [_serialize_part(part) for part in db.parts.find(query).sort("part_name", 1)]
    has_unspecified_manufacturer = any(not str(part.get("manufacturer") or "").strip() for part in parts)

    return render_template(
        "services/manage_parts.html",
        parts=parts,
        part_categories=_build_filter_values(parts, "category"),
        part_manufacturers=_build_filter_values(parts, "manufacturer"),
        part_codes=_build_filter_values(parts, "part_code"),
        has_unspecified_manufacturer=has_unspecified_manufacturer,
    )


@bp.route("/parts/export/csv")
def export_parts_csv():
    db = ensure_connection_or_500()
    business_id = _resolve_current_business_id(db)
    query = {"business_id": business_id} if business_id else {"_id": None}
    rows = list(db.parts.find(query).sort("part_name", 1))
    return build_csv_export_response(rows, "parts_export.csv")


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
    materials = [_serialize_material(material) for material in db.materials.find(query).sort("material_name", 1)]

    return render_template(
        "services/manage_materials.html",
        materials=materials,
        material_categories=_build_filter_values(materials, "category"),
        material_manufacturers=_build_filter_values(materials, "manufacturer"),
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
        equipment_categories=_build_filter_values(equipment_items, "category"),
        equipment_manufacturers=_build_filter_values(equipment_items, "manufacturer"),
    )


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
    selected_part_ids = []
    selected_material_ids = []
    selected_equipment_ids = []
    service_part_rows = _build_service_part_rows()
    service_material_rows = _build_service_material_rows()
    service_equipment_rows = _build_service_equipment_rows()
    category_options = _build_category_options(db, "services", "category", business_id)

    if request.method == "POST":
        form_data = _service_form_data(request.form)
        selected_part_ids = request.form.getlist("part_id[]")
        selected_material_ids = request.form.getlist("material_id[]")
        selected_equipment_ids = request.form.getlist("equipment_id[]")
        entered_part_costs = request.form.getlist("part_cost_display[]")
        entered_material_quantities = request.form.getlist("material_default_quantity_display[]")
        entered_material_units = request.form.getlist("material_unit_of_measure_display[]")
        entered_material_prices = request.form.getlist("material_price_display[]")
        service_part_rows = _build_service_part_rows(selected_part_ids, entered_part_costs)
        service_material_rows = _build_service_material_rows(
            selected_material_ids,
            entered_material_quantities,
            entered_material_units,
            entered_material_prices,
        )
        service_equipment_rows = _build_service_equipment_rows(selected_equipment_ids)
        materials_cost, error = _parse_optional_nonnegative_float(form_data["materials_cost"], "Materials Cost", default=0.0)
        if not error:
            estimated_hours, error = _parse_nonnegative_float(form_data["estimated_hours"], "Estimated Hours")
        if not error:
            standard_price, error = _parse_nonnegative_float(form_data["standard_price"], "Standard Price")
        if not error:
            emergency_price, error = _parse_nonnegative_float(form_data["emergency_price"], "Emergency Price")
        if not error:
            service_part_entries, valid_part_ids, error = _parse_service_part_entries(selected_part_ids, entered_part_costs)
        if not error:
            service_material_entries, valid_material_ids, error = _parse_service_material_entries(
                selected_material_ids,
                entered_material_quantities,
                entered_material_units,
                entered_material_prices,
            )
        if not error:
            service_equipment_entries, valid_equipment_ids, error = _parse_service_equipment_entries(selected_equipment_ids)

        if not error and not form_data["service_name"]:
            error = "Service Name is required."
        elif not error and not form_data["service_type"]:
            error = "Service Type is required."
        elif not error and form_data["service_type"] not in SERVICE_TYPE_OPTIONS:
            error = "Please select a valid Service Type."
        elif not error and not form_data["service_code"]:
            error = "Service Code is required."
        elif not error and not form_data["category"]:
            error = "Category is required."
        elif not error and db.services.find_one({"business_id": business_id, "service_code": form_data["service_code"]}):
            error = "A service with that code already exists."

        if not error:
            emergency_enabled = _parse_boolean(form_data["emergency"])
            db.services.insert_one(
                {
                    "business_id": business_id,
                    "service_name": form_data["service_name"],
                    "service_type": form_data["service_type"],
                    "service_code": form_data["service_code"],
                    "category": form_data["category"],
                    "description": form_data["description"],
                    "materials_cost": materials_cost,
                    "estimated_hours": estimated_hours,
                    "standard_price": standard_price,
                    "emergency": emergency_enabled,
                    "emergency_price": emergency_price,
                    "part_ids": valid_part_ids,
                    "service_parts": service_part_entries,
                    "material_ids": valid_material_ids,
                    "service_materials": service_material_entries,
                    "equipment_ids": valid_equipment_ids,
                    "service_equipment": service_equipment_entries,
                }
            )
            return redirect(url_for("catalog.manage_services"))

    part_query = {"business_id": business_id}
    material_query = {"business_id": business_id}
    equipment_query = {"business_id": business_id}
    parts = [_serialize_part(part) for part in db.parts.find(part_query).sort("part_name", 1)]
    materials = [_serialize_material(material) for material in db.materials.find(material_query).sort("material_name", 1)]
    equipment_items = [_serialize_equipment(eq) for eq in db.equipment.find(equipment_query).sort("equipment_name", 1)]
    parts_catalog_by_id = {
        part["_id"]: {"unit_cost": part["unit_cost_display"], "part_name": part["part_name"], "part_code": part["part_code"]}
        for part in parts
    }
    materials_catalog_by_id = {
        material["_id"]: {
            "material_name": material["material_name"],
            "default_quantity_used": material["default_quantity_used_display"],
            "unit_of_measure": material["unit_of_measure"],
            "price": material["price_display"],
        }
        for material in materials
    }
    return render_template(
        "services/create_service.html",
        error=error,
        form_data=form_data,
        parts=parts,
        materials=materials,
        equipment_items=equipment_items,
        parts_catalog_by_id=parts_catalog_by_id,
        materials_catalog_by_id=materials_catalog_by_id,
        selected_part_ids=selected_part_ids,
        selected_material_ids=selected_material_ids,
        selected_equipment_ids=selected_equipment_ids,
        service_part_rows=service_part_rows,
        service_material_rows=service_material_rows,
        service_equipment_rows=service_equipment_rows,
        category_options=category_options,
        service_type_options=SERVICE_TYPE_OPTIONS,
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

    service_part_entries = service.get("service_parts") or []
    part_ids = [entry.get("part_id") for entry in service_part_entries if entry.get("part_id")] or service.get("part_ids", [])
    associated_parts = []
    part_entry_by_id = {str(entry.get("part_id")): entry for entry in service_part_entries if entry.get("part_id")}
    for pid in part_ids:
        if pid and ObjectId.is_valid(str(pid)):
            part_doc = db.parts.find_one({"_id": ObjectId(str(pid))})
            if part_doc:
                serialized_part = _serialize_part(part_doc)
                part_entry = part_entry_by_id.get(str(pid))
                if part_entry:
                    serialized_part["unit_cost_display"] = _format_currency_display(part_entry.get("unit_cost"))
                associated_parts.append(serialized_part)

    service_material_entries = service.get("service_materials") or []
    material_ids = [entry.get("material_id") for entry in service_material_entries if entry.get("material_id")] or service.get("material_ids", [])
    associated_materials = []
    material_entry_by_id = {str(entry.get("material_id")): entry for entry in service_material_entries if entry.get("material_id")}
    for mid in material_ids:
        if mid and ObjectId.is_valid(str(mid)):
            material_doc = db.materials.find_one({"_id": ObjectId(str(mid))})
            if material_doc:
                serialized_material = _serialize_material(material_doc)
                material_entry = material_entry_by_id.get(str(mid))
                if material_entry:
                    if material_entry.get("default_quantity_used") is not None:
                        serialized_material["default_quantity_used_display"] = _format_hours_display(material_entry.get("default_quantity_used"))
                    serialized_material["unit_of_measure"] = str(material_entry.get("unit_of_measure") or serialized_material.get("unit_of_measure") or "").strip()
                    serialized_material["price_display"] = _format_currency_display(material_entry.get("price"))
                associated_materials.append(serialized_material)

    service_equipment_entries = service.get("service_equipment") or []
    equipment_ids_from_entries = [entry.get("equipment_id") for entry in service_equipment_entries if entry.get("equipment_id")] or service.get("equipment_ids", [])
    associated_equipment = []
    for eid in equipment_ids_from_entries:
        if eid and ObjectId.is_valid(str(eid)):
            equipment_doc = db.equipment.find_one({"_id": ObjectId(str(eid))})
            if equipment_doc:
                associated_equipment.append(_serialize_equipment(equipment_doc))

    return render_template(
        "services/view_service.html",
        serviceId=serviceId,
        service=_serialize_service(service),
        associated_parts=associated_parts,
        associated_materials=associated_materials,
        associated_equipment=associated_equipment,
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
    selected_part_ids = [str(pid) for pid in service.get("part_ids", [])]
    selected_material_ids = [str(mid) for mid in service.get("material_ids", [])]
    selected_equipment_ids = [str(eid) for eid in service.get("equipment_ids", [])]
    service_part_rows = _build_service_part_rows(service_part_entries=service.get("service_parts") or [])
    service_material_rows = _build_service_material_rows(service_material_entries=service.get("service_materials") or [])
    service_equipment_rows = _build_service_equipment_rows(service_equipment_entries=service.get("service_equipment") or [])
    category_options = _build_category_options(db, "services", "category", business_id)

    if request.method == "POST":
        form_data = _service_form_data(request.form)
        selected_part_ids = request.form.getlist("part_id[]")
        selected_material_ids = request.form.getlist("material_id[]")
        selected_equipment_ids = request.form.getlist("equipment_id[]")
        entered_part_costs = request.form.getlist("part_cost_display[]")
        entered_material_quantities = request.form.getlist("material_default_quantity_display[]")
        entered_material_units = request.form.getlist("material_unit_of_measure_display[]")
        entered_material_prices = request.form.getlist("material_price_display[]")
        service_part_rows = _build_service_part_rows(selected_part_ids, entered_part_costs)
        service_material_rows = _build_service_material_rows(
            selected_material_ids,
            entered_material_quantities,
            entered_material_units,
            entered_material_prices,
        )
        service_equipment_rows = _build_service_equipment_rows(selected_equipment_ids)
        materials_cost, error = _parse_optional_nonnegative_float(
            form_data["materials_cost"],
            "Materials Cost",
            default=service.get("materials_cost", 0.0),
        )
        if not error:
            estimated_hours, error = _parse_nonnegative_float(form_data["estimated_hours"], "Estimated Hours")
        if not error:
            standard_price, error = _parse_nonnegative_float(form_data["standard_price"], "Standard Price")
        if not error:
            emergency_price, error = _parse_nonnegative_float(form_data["emergency_price"], "Emergency Price")
        if not error:
            service_part_entries, valid_part_ids, error = _parse_service_part_entries(selected_part_ids, entered_part_costs)
        if not error:
            service_material_entries, valid_material_ids, error = _parse_service_material_entries(
                selected_material_ids,
                entered_material_quantities,
                entered_material_units,
                entered_material_prices,
            )
        if not error:
            service_equipment_entries, valid_equipment_ids, error = _parse_service_equipment_entries(selected_equipment_ids)

        if not error and not form_data["service_name"]:
            error = "Service Name is required."
        elif not error and not form_data["service_type"]:
            error = "Service Type is required."
        elif not error and form_data["service_type"] not in SERVICE_TYPE_OPTIONS:
            error = "Please select a valid Service Type."
        elif not error and not form_data["service_code"]:
            error = "Service Code is required."
        elif not error and not form_data["category"]:
            error = "Category is required."
        elif not error and db.services.find_one({
            "business_id": business_id,
            "service_code": form_data["service_code"],
            "_id": {"$ne": object_id_or_404(serviceId)},
        }):
            error = "A service with that code already exists."

        if not error:
            emergency_enabled = _parse_boolean(form_data["emergency"])
            db.services.update_one(
                query,
                {"$set": {
                    "service_name": form_data["service_name"],
                    "service_type": form_data["service_type"],
                    "service_code": form_data["service_code"],
                    "category": form_data["category"],
                    "description": form_data["description"],
                    "materials_cost": materials_cost,
                    "estimated_hours": estimated_hours,
                    "standard_price": standard_price,
                    "emergency": emergency_enabled,
                    "emergency_price": emergency_price,
                    "part_ids": valid_part_ids,
                    "service_parts": service_part_entries,
                    "material_ids": valid_material_ids,
                    "service_materials": service_material_entries,
                    "equipment_ids": valid_equipment_ids,
                    "service_equipment": service_equipment_entries,
                }},
            )
            return redirect(url_for("catalog.view_service", serviceId=serviceId))

    part_query = {"business_id": business_id}
    material_query = {"business_id": business_id}
    equipment_query = {"business_id": business_id}
    parts = [_serialize_part(part) for part in db.parts.find(part_query).sort("part_name", 1)]
    materials = [_serialize_material(material) for material in db.materials.find(material_query).sort("material_name", 1)]
    equipment_items = [_serialize_equipment(eq) for eq in db.equipment.find(equipment_query).sort("equipment_name", 1)]
    parts_catalog_by_id = {
        part["_id"]: {"unit_cost": part["unit_cost_display"], "part_name": part["part_name"], "part_code": part["part_code"]}
        for part in parts
    }
    materials_catalog_by_id = {
        material["_id"]: {
            "material_name": material["material_name"],
            "default_quantity_used": material["default_quantity_used_display"],
            "unit_of_measure": material["unit_of_measure"],
            "price": material["price_display"],
        }
        for material in materials
    }
    return render_template(
        "services/update_service.html",
        serviceId=serviceId,
        error=error,
        form_data=form_data,
        parts=parts,
        materials=materials,
        equipment_items=equipment_items,
        parts_catalog_by_id=parts_catalog_by_id,
        materials_catalog_by_id=materials_catalog_by_id,
        selected_part_ids=selected_part_ids,
        selected_material_ids=selected_material_ids,
        selected_equipment_ids=selected_equipment_ids,
        service_part_rows=service_part_rows,
        service_material_rows=service_material_rows,
        service_equipment_rows=service_equipment_rows,
        category_options=category_options,
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
    category_options = _build_category_options(db, "parts", "category", business_id)

    if request.method == "POST":
        form_data = _part_form_data(request.form)
        unit_cost, error = _parse_nonnegative_float(form_data["unit_cost"], "Unit Cost")

        if not error and not form_data["part_name"]:
            error = "Part Name is required."
        elif not error and not form_data["part_code"]:
            error = "Part Code is required."
        elif not error and not form_data["category"]:
            error = "Category is required."
        elif not error and not form_data["manufacturer"]:
            error = "Manufacturer is required."
        elif not error and db.parts.find_one({"business_id": business_id, "part_code": form_data["part_code"]}):
            error = "A part with that code already exists."

        if not error:
            db.parts.insert_one(
                {
                    "business_id": business_id,
                    "part_name": form_data["part_name"],
                    "part_code": form_data["part_code"],
                    "category": form_data["category"],
                    "manufacturer": form_data["manufacturer"],
                    "description": form_data["description"],
                    "unit_cost": unit_cost,
                    "purchase_link": form_data["purchase_link"],
                }
            )
            return redirect(url_for("catalog.manage_parts"))

    return render_template("services/create_part.html", error=error, form_data=form_data, category_options=category_options)


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
    category_options = _build_category_options(db, "parts", "category", business_id)

    if request.method == "POST":
        form_data = _part_form_data(request.form)
        unit_cost, error = _parse_nonnegative_float(form_data["unit_cost"], "Unit Cost")

        if not error and not form_data["part_name"]:
            error = "Part Name is required."
        elif not error and not form_data["part_code"]:
            error = "Part Code is required."
        elif not error and not form_data["category"]:
            error = "Category is required."
        elif not error and not form_data["manufacturer"]:
            error = "Manufacturer is required."
        elif not error and db.parts.find_one({
            "business_id": business_id,
            "part_code": form_data["part_code"],
            "_id": {"$ne": object_id_or_404(partId)},
        }):
            error = "A part with that code already exists."

        if not error:
            db.parts.update_one(
                query,
                {"$set": {
                    "part_name": form_data["part_name"],
                    "part_code": form_data["part_code"],
                    "category": form_data["category"],
                    "manufacturer": form_data["manufacturer"],
                    "description": form_data["description"],
                    "unit_cost": unit_cost,
                    "purchase_link": form_data["purchase_link"],
                }},
            )
            return redirect(url_for("catalog.view_part", partId=partId))

    return render_template("services/update_part.html", partId=partId, error=error, form_data=form_data, category_options=category_options)


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
    category_options = _build_category_options(db, "materials", "category", business_id)

    if request.method == "POST":
        form_data = _material_form_data(request.form)
        default_quantity_used, error = _parse_optional_nonnegative_float(form_data["default_quantity_used"], "Default Quantity Used")
        if not error:
            price, error = _parse_optional_nonnegative_float(form_data["price"], "Price")

        if not error and not form_data["material_name"]:
            error = "Material Name is required."

        if not error:
            db.materials.insert_one(
                {
                    "business_id": business_id,
                    "material_name": form_data["material_name"],
                    "category": form_data["category"],
                    "part_number": form_data["part_number"],
                    "manufacturer": form_data["manufacturer"],
                    "default_quantity_used": default_quantity_used,
                    "unit_of_measure": form_data["unit_of_measure"],
                    "price": price,
                    "purchase_link": form_data["purchase_link"],
                }
            )
            return redirect(url_for("catalog.manage_materials"))

    return render_template("services/create_materials.html", error=error, form_data=form_data, uom_options=uom_options, category_options=category_options)


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
    category_options = _build_category_options(db, "materials", "category", business_id)

    if request.method == "POST":
        form_data = _material_form_data(request.form)
        default_quantity_used, error = _parse_optional_nonnegative_float(form_data["default_quantity_used"], "Default Quantity Used")
        if not error:
            price, error = _parse_optional_nonnegative_float(form_data["price"], "Price")

        if not error and not form_data["material_name"]:
            error = "Material Name is required."

        if not error:
            db.materials.update_one(
                query,
                {
                    "$set": {
                        "material_name": form_data["material_name"],
                        "category": form_data["category"],
                        "part_number": form_data["part_number"],
                        "manufacturer": form_data["manufacturer"],
                        "default_quantity_used": default_quantity_used,
                        "unit_of_measure": form_data["unit_of_measure"],
                        "price": price,
                        "purchase_link": form_data["purchase_link"],
                    }
                },
            )
            return redirect(url_for("catalog.view_material", materialId=materialId))

    return render_template("services/update_materials.html", materialId=materialId, error=error, form_data=form_data, uom_options=uom_options, category_options=category_options)


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
    category_options = _build_category_options(db, "equipment", "category", business_id)

    if request.method == "POST":
        form_data = _equipment_form_data(request.form)
        default_price, error = _parse_optional_nonnegative_float(form_data["default_price"], "Default Price")
        if not error:
            default_quantity_installed, error = _parse_optional_nonnegative_float(form_data["default_quantity_installed"], "Default Quantity Installed")

        if not error and not form_data["equipment_name"]:
            error = "Equipment Name is required."

        if not error:
            db.equipment.insert_one(
                {
                    "business_id": business_id,
                    "equipment_name": form_data["equipment_name"],
                    "equipment_type": form_data["equipment_type"],
                    "manufacturer": form_data["manufacturer"],
                    "category": form_data["category"],
                    "sku": form_data["sku"],
                    "description": form_data["description"],
                    "notes": form_data["notes"],
                    "purchase_link": form_data["purchase_link"],
                    "default_price": default_price,
                    "default_quantity_installed": default_quantity_installed,
                }
            )
            return redirect(url_for("catalog.manage_equipment"))

    return render_template("services/create_equipment.html", error=error, form_data=form_data, category_options=category_options, equipment_type_options=EQUIPMENT_TYPE_OPTIONS)


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
    category_options = _build_category_options(db, "equipment", "category", business_id)

    if request.method == "POST":
        form_data = _equipment_form_data(request.form)
        default_price, error = _parse_optional_nonnegative_float(form_data["default_price"], "Default Price")
        if not error:
            default_quantity_installed, error = _parse_optional_nonnegative_float(form_data["default_quantity_installed"], "Default Quantity Installed")

        if not error and not form_data["equipment_name"]:
            error = "Equipment Name is required."

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
                        "description": form_data["description"],
                        "notes": form_data["notes"],
                        "purchase_link": form_data["purchase_link"],
                        "default_price": default_price,
                        "default_quantity_installed": default_quantity_installed,
                    }
                },
            )
            return redirect(url_for("catalog.view_equipment", equipmentId=equipmentId))

    return render_template("services/update_equipment.html", equipmentId=equipmentId, error=error, form_data=form_data, category_options=category_options, equipment_type_options=EQUIPMENT_TYPE_OPTIONS)


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
