from bson import ObjectId
from flask import Blueprint, redirect, render_template, request, session, url_for

from mongo import ensure_connection_or_500, object_id_or_404, serialize_doc

bp = Blueprint("catalog", __name__)


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
    serialized["category"] = str(serialized.get("category") or "").strip()
    serialized["materials_cost_display"] = _format_currency_display(serialized.get("materials_cost"))
    serialized["standard_price_display"] = _format_currency_display(serialized.get("standard_price"))
    serialized["estimated_hours_display"] = _format_hours_display(serialized.get("estimated_hours"))
    return serialized


def _serialize_part(part):
    serialized = serialize_doc(part)
    serialized["category"] = str(serialized.get("category") or "").strip()
    serialized["unit_cost_display"] = _format_currency_display(serialized.get("unit_cost"))
    return serialized


def _build_filter_values(items, key):
    values = sorted({str(item.get(key) or "").strip() for item in items if str(item.get(key) or "").strip()})
    return values


def _service_form_data(service=None):
    service = service or {}
    return {
        "service_name": str(service.get("service_name") or "").strip(),
        "service_code": str(service.get("service_code") or "").strip(),
        "category": str(service.get("category") or "").strip(),
        "description": str(service.get("description") or "").strip(),
        "materials_cost": str(service.get("materials_cost") or "").strip(),
        "estimated_hours": str(service.get("estimated_hours") or "").strip(),
        "standard_price": str(service.get("standard_price") or "").strip(),
    }


def _part_form_data(part=None):
    part = part or {}
    return {
        "part_name": str(part.get("part_name") or "").strip(),
        "part_code": str(part.get("part_code") or "").strip(),
        "category": str(part.get("category") or "").strip(),
        "description": str(part.get("description") or "").strip(),
        "unit_cost": str(part.get("unit_cost") or "").strip(),
    }


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


@bp.route("/parts")
def manage_parts():
    db = ensure_connection_or_500()
    business_id = _resolve_current_business_id(db)
    query = {"business_id": business_id} if business_id else {"_id": None}
    parts = [_serialize_part(part) for part in db.parts.find(query).sort("part_name", 1)]

    return render_template(
        "services/manage_parts.html",
        parts=parts,
        part_categories=_build_filter_values(parts, "category"),
        part_codes=_build_filter_values(parts, "part_code"),
    )


@bp.route("/services/create", methods=["GET", "POST"])
def create_service():
    db = ensure_connection_or_500()
    business_id = _resolve_current_business_id(db)
    if not business_id:
        return redirect(url_for("admin_bp.admin"))

    error = ""
    form_data = _service_form_data()

    if request.method == "POST":
        form_data = _service_form_data(request.form)
        materials_cost, error = _parse_nonnegative_float(form_data["materials_cost"], "Materials Cost")
        if not error:
            estimated_hours, error = _parse_nonnegative_float(form_data["estimated_hours"], "Estimated Hours")
        if not error:
            standard_price, error = _parse_nonnegative_float(form_data["standard_price"], "Standard Price")

        if not error and not form_data["service_name"]:
            error = "Service Name is required."
        elif not error and not form_data["service_code"]:
            error = "Service Code is required."
        elif not error and not form_data["category"]:
            error = "Category is required."
        elif not error and db.services.find_one({"business_id": business_id, "service_code": form_data["service_code"]}):
            error = "A service with that code already exists."

        if not error:
            db.services.insert_one(
                {
                    "business_id": business_id,
                    "service_name": form_data["service_name"],
                    "service_code": form_data["service_code"],
                    "category": form_data["category"],
                    "description": form_data["description"],
                    "materials_cost": materials_cost,
                    "estimated_hours": estimated_hours,
                    "standard_price": standard_price,
                }
            )
            return redirect(url_for("catalog.manage_services"))

    return render_template("services/create_service.html", error=error, form_data=form_data)


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

    return render_template("services/view_service.html", serviceId=serviceId, service=_serialize_service(service))


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

    if request.method == "POST":
        form_data = _service_form_data(request.form)
        materials_cost, error = _parse_nonnegative_float(form_data["materials_cost"], "Materials Cost")
        if not error:
            estimated_hours, error = _parse_nonnegative_float(form_data["estimated_hours"], "Estimated Hours")
        if not error:
            standard_price, error = _parse_nonnegative_float(form_data["standard_price"], "Standard Price")

        if not error and not form_data["service_name"]:
            error = "Service Name is required."
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
            db.services.update_one(
                query,
                {"$set": {
                    "service_name": form_data["service_name"],
                    "service_code": form_data["service_code"],
                    "category": form_data["category"],
                    "description": form_data["description"],
                    "materials_cost": materials_cost,
                    "estimated_hours": estimated_hours,
                    "standard_price": standard_price,
                }},
            )
            return redirect(url_for("catalog.view_service", serviceId=serviceId))

    return render_template(
        "services/update_service.html",
        serviceId=serviceId,
        error=error,
        form_data=form_data,
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

    if request.method == "POST":
        form_data = _part_form_data(request.form)
        unit_cost, error = _parse_nonnegative_float(form_data["unit_cost"], "Unit Cost")

        if not error and not form_data["part_name"]:
            error = "Part Name is required."
        elif not error and not form_data["part_code"]:
            error = "Part Code is required."
        elif not error and not form_data["category"]:
            error = "Category is required."
        elif not error and db.parts.find_one({"business_id": business_id, "part_code": form_data["part_code"]}):
            error = "A part with that code already exists."

        if not error:
            db.parts.insert_one(
                {
                    "business_id": business_id,
                    "part_name": form_data["part_name"],
                    "part_code": form_data["part_code"],
                    "category": form_data["category"],
                    "description": form_data["description"],
                    "unit_cost": unit_cost,
                }
            )
            return redirect(url_for("catalog.manage_parts"))

    return render_template("services/create_part.html", error=error, form_data=form_data)


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

    if request.method == "POST":
        form_data = _part_form_data(request.form)
        unit_cost, error = _parse_nonnegative_float(form_data["unit_cost"], "Unit Cost")

        if not error and not form_data["part_name"]:
            error = "Part Name is required."
        elif not error and not form_data["part_code"]:
            error = "Part Code is required."
        elif not error and not form_data["category"]:
            error = "Category is required."
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
                    "description": form_data["description"],
                    "unit_cost": unit_cost,
                }},
            )
            return redirect(url_for("catalog.view_part", partId=partId))

    return render_template("services/update_part.html", partId=partId, error=error, form_data=form_data)


@bp.route("/parts/<partId>/delete", methods=["POST"])
def delete_part(partId):
    db = ensure_connection_or_500()
    business_id = _resolve_current_business_id(db)
    query = {"_id": object_id_or_404(partId)}
    if business_id:
        query["business_id"] = business_id
    db.parts.delete_one(query)
    return redirect(url_for("catalog.manage_parts"))
