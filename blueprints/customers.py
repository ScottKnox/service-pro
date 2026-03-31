from datetime import datetime
import json
import re

from bson import ObjectId
from flask import Blueprint, current_app, redirect, render_template, request, session, url_for

from mongo import ensure_connection_or_500, object_id_or_404, serialize_doc
from utils.catalog import build_job_parts_from_form, build_part_catalog

bp = Blueprint("customers", __name__)

EMAIL_VALIDATION_MESSAGE = "Enter a valid email address."
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _email_is_valid(email):
    return bool(EMAIL_PATTERN.match(email))


@bp.route("/customers")
def customers():
    db = ensure_connection_or_500()
    customers_list = [
        serialize_doc(customer)
        for customer in db.customers.find().sort([("last_name", 1), ("first_name", 1)])
    ]
    return render_template("customers/customers.html", customers=customers_list)


@bp.route("/customers/add", methods=["GET", "POST"])
def add_customer():
    db = ensure_connection_or_500()
    if request.method == "POST":
        form_data = request.form.to_dict()
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        company = request.form.get("company", "").strip()
        phone = request.form.get("phone", "").strip()
        email = request.form.get("email", "").strip()
        address_line_1 = request.form.get("address_line_1", "").strip()
        address_line_2 = request.form.get("address_line_2", "").strip()
        city = request.form.get("city", "").strip()
        state = request.form.get("state", "").strip().upper()
        referral_source = request.form.get("referral_source", "").strip()

        if not first_name or not last_name:
            return render_template(
                "customers/add_customer.html",
                error="First name and last name are required.",
                form_data=form_data,
            )

        if email and not _email_is_valid(email):
            return render_template(
                "customers/add_customer.html",
                error=EMAIL_VALIDATION_MESSAGE,
                form_data=form_data,
            )

        customer_status = "Active" if all((phone, email, address_line_1, city, state)) else "Lead"

        customer_count = db.customers.count_documents({}) + 1
        customer = {
            "first_name": first_name,
            "last_name": last_name,
            "company": company,
            "phone": phone,
            "email": email,
            "address_line_1": address_line_1,
            "address_line_2": address_line_2,
            "city": city,
            "state": state,
            "referral_source": referral_source,
            "customer_status": customer_status,
            "date_added": datetime.now().strftime("%m/%d/%Y"),
            "account_number": f"ACC-{customer_count:05d}",
            "account_type": "Residential",
            "balance_due": "$0.00",
            "account_status": "Current",
        }
        inserted = db.customers.insert_one(customer)
        current_app.logger.info("Customer created: id=%s by employee_id=%s", str(inserted.inserted_id), session.get("employee_id"))
        return redirect(url_for("customers.view_customer", customerId=str(inserted.inserted_id)))

    return render_template("customers/add_customer.html", error="", form_data={})


@bp.route("/customers/<customerId>/update", methods=["GET", "POST"])
def update_customer(customerId):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return redirect(url_for("customers.customers"))

    if request.method == "POST":
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        phone = request.form.get("phone", "").strip()
        email = request.form.get("email", "").strip()
        address_line_1 = request.form.get("address_line_1", "").strip()
        city = request.form.get("city", "").strip()
        state = request.form.get("state", "").strip().upper()

        if not first_name or not last_name:
            return render_template(
                "customers/update_customer.html",
                customerId=customerId,
                customer=serialize_doc(customer),
                error="First name and last name are required.",
            )

        current_status = str(customer.get("customer_status", "")).strip()
        next_status = current_status
        if current_status.lower() == "lead" and all((phone, email, address_line_1, city, state)):
            next_status = "Active"

        update_data = {
            "first_name": first_name,
            "last_name": last_name,
            "company": request.form.get("company", "").strip(),
            "phone": phone,
            "email": email,
            "address_line_1": address_line_1,
            "address_line_2": request.form.get("address_line_2", "").strip(),
            "city": city,
            "state": state,
            "referral_source": request.form.get("referral_source", "").strip(),
            "customer_status": next_status,
        }

        db.customers.update_one({"_id": ObjectId(customerId)}, {"$set": update_data})
        return redirect(url_for("customers.view_customer", customerId=customerId))

    return render_template(
        "customers/update_customer.html",
        customerId=customerId,
        customer=serialize_doc(customer),
        error="",
    )


@bp.route("/customers/<customerId>/delete", methods=["POST"])
def delete_customer(customerId):
    db = ensure_connection_or_500()
    customer_oid = object_id_or_404(customerId)
    customer = db.customers.find_one({"_id": customer_oid})
    if not customer:
        return redirect(url_for("customers.customers"))

    related_jobs = list(db.jobs.find({"customer_id": customerId}, {"_id": 1}))
    related_job_ids = [str(job.get("_id")) for job in related_jobs]

    db.customers.delete_one({"_id": customer_oid})
    db.jobs.delete_many({"customer_id": customerId})
    db.equipment.delete_many({"customer_id": customerId})
    if related_job_ids:
        db.estimates.delete_many({"job_id": {"$in": related_job_ids}})
    current_app.logger.info("Customer deleted: id=%s by employee_id=%s", customerId, session.get("employee_id"))
    return redirect(url_for("customers.customers"))


@bp.route("/customers/<customerId>")
def view_customer(customerId):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return redirect(url_for("customers.customers"))

    jobs_page_raw = request.args.get("jobs_page", "1")
    payments_page_raw = request.args.get("payments_page", "1")
    equipment_page_raw = request.args.get("equipment_page", "1")

    try:
        jobs_page = max(1, int(jobs_page_raw))
    except ValueError:
        jobs_page = 1

    try:
        payments_page = max(1, int(payments_page_raw))
    except ValueError:
        payments_page = 1

    try:
        equipment_page = max(1, int(equipment_page_raw))
    except ValueError:
        equipment_page = 1

    jobs_per_page = 5
    payments_per_page = 5
    equipment_per_page = 5

    customer_jobs_total = db.jobs.count_documents({"customer_id": customerId})
    customer_jobs_total_pages = (customer_jobs_total + jobs_per_page - 1) // jobs_per_page
    if customer_jobs_total_pages == 0:
        jobs_page = 1
    elif jobs_page > customer_jobs_total_pages:
        jobs_page = customer_jobs_total_pages

    customer_payments_total = db.payments.count_documents({"customer_id": customerId})
    customer_payments_total_pages = (customer_payments_total + payments_per_page - 1) // payments_per_page
    if customer_payments_total_pages == 0:
        payments_page = 1
    elif payments_page > customer_payments_total_pages:
        payments_page = customer_payments_total_pages

    customer_equipment_total = db.equipment.count_documents({"customer_id": customerId})
    customer_equipment_total_pages = (customer_equipment_total + equipment_per_page - 1) // equipment_per_page
    if customer_equipment_total_pages == 0:
        equipment_page = 1
    elif equipment_page > customer_equipment_total_pages:
        equipment_page = customer_equipment_total_pages

    customer_pages = {
        "jobs": jobs_page,
        "payments": payments_page,
        "equipment": equipment_page,
    }

    jobs_skip = (jobs_page - 1) * jobs_per_page
    customer_jobs = [
        serialize_doc(job)
        for job in db.jobs.find({"customer_id": customerId}).sort([("scheduled_date", -1), ("scheduled_time", -1)]).skip(jobs_skip).limit(jobs_per_page)
    ]

    payments_skip = (payments_page - 1) * payments_per_page
    customer_payments = [
        serialize_doc(payment)
        for payment in db.payments.find({"customer_id": customerId}).sort([("date", -1), ("_id", -1)]).skip(payments_skip).limit(payments_per_page)
    ]
    equipment_skip = (equipment_page - 1) * equipment_per_page
    customer_equipment = [
        serialize_doc(equipment)
        for equipment in db.equipment.find({"customer_id": customerId}).sort([("equipment_name", 1), ("_id", -1)]).skip(equipment_skip).limit(equipment_per_page)
    ]

    return render_template(
        "customers/view_customer.html",
        customerId=customerId,
        customer=serialize_doc(customer),
        customer_pages=customer_pages,
        customer_jobs=customer_jobs,
        customer_jobs_total_pages=customer_jobs_total_pages,
        customer_payments=customer_payments,
        customer_payments_total_pages=customer_payments_total_pages,
        customer_equipment=customer_equipment,
        customer_equipment_total_pages=customer_equipment_total_pages,
    )


@bp.route("/customers/<customerId>/equipment/add", methods=["GET", "POST"])
def add_equipment(customerId):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return redirect(url_for("customers.customers"))

    error = ""
    part_docs = [serialize_doc(part) for part in db.parts.find().sort("part_name", 1)]
    part_catalog = build_part_catalog(part_docs)
    if request.method == "POST":
        equipment_name = request.form.get("equipment_name", "").strip()
        serial_number = request.form.get("serial_number", "").strip()
        brand = request.form.get("brand", "").strip()
        equipment_location = request.form.get("equipment_location", "").strip()
        equipment_notes = request.form.get("equipment_notes", "").strip()
        selected_part_names = request.form.getlist("part_name[]")
        entered_part_prices = request.form.getlist("part_price[]")
        equipment_parts, _ = build_job_parts_from_form(selected_part_names, entered_part_prices, part_catalog)

        if not equipment_name:
            error = "Equipment type is required."
        elif not brand:
            error = "Brand is required."
        elif not equipment_location:
            error = "Equipment location is required."
        else:
            equipment = {
                "customer_id": customerId,
                "equipment_name": equipment_name,
                "serial_number": serial_number,
                "brand": brand,
                "equipment_location": equipment_location,
                "notes": equipment_notes,
                "parts": equipment_parts,
            }
            inserted = db.equipment.insert_one(equipment)
            return redirect(url_for("customers.view_equipment", customerId=customerId, equipmentId=str(inserted.inserted_id)))

    return render_template(
        "equipment/add_equipment.html",
        customerId=customerId,
        customer=serialize_doc(customer),
        parts=part_docs,
        parts_catalog_json=json.dumps(part_catalog),
        error=error,
    )


@bp.route("/customers/<customerId>/equipment/<equipmentId>")
def view_equipment(customerId, equipmentId):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return redirect(url_for("customers.customers"))

    equipment = db.equipment.find_one({"_id": object_id_or_404(equipmentId), "customer_id": customerId})
    if not equipment:
        return redirect(url_for("customers.view_customer", customerId=customerId))

    serialized_equipment = serialize_doc(equipment)
    equipment_part_names = [
        (part.get("name") or "").strip()
        for part in serialized_equipment.get("parts", [])
        if (part.get("name") or "").strip()
    ]
    part_lookup = {}

    if equipment_part_names:
        matching_parts = [
            serialize_doc(part)
            for part in db.parts.find({"part_name": {"$in": equipment_part_names}})
        ]
        part_lookup = {part.get("part_name"): part for part in matching_parts}

    equipment_parts = []
    for part in serialized_equipment.get("parts", []):
        part_name = (part.get("name") or "").strip()
        matched_part = part_lookup.get(part_name)
        equipment_parts.append(
            {
                "name": part_name or "-",
                "price": part.get("price", ""),
                "part_id": matched_part.get("_id") if matched_part else None,
                "product_link": matched_part.get("product_link", "") if matched_part else "",
            }
        )

    return render_template(
        "equipment/view_equipment.html",
        customerId=customerId,
        equipmentId=equipmentId,
        customer=serialize_doc(customer),
        equipment=serialized_equipment,
        equipment_parts=equipment_parts,
    )


@bp.route("/customers/<customerId>/equipment/<equipmentId>/update", methods=["GET", "POST"])
def update_equipment(customerId, equipmentId):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return redirect(url_for("customers.customers"))

    equipment = db.equipment.find_one({"_id": object_id_or_404(equipmentId), "customer_id": customerId})
    if not equipment:
        return redirect(url_for("customers.view_customer", customerId=customerId))

    error = ""
    part_docs = [serialize_doc(part) for part in db.parts.find().sort("part_name", 1)]
    part_catalog = build_part_catalog(part_docs)
    if request.method == "POST":
        equipment_name = request.form.get("equipment_name", "").strip()
        serial_number = request.form.get("serial_number", "").strip()
        brand = request.form.get("brand", "").strip()
        equipment_location = request.form.get("equipment_location", "").strip()
        equipment_notes = request.form.get("equipment_notes", "").strip()
        selected_part_names = request.form.getlist("part_name[]")
        entered_part_prices = request.form.getlist("part_price[]")
        equipment_parts, _ = build_job_parts_from_form(selected_part_names, entered_part_prices, part_catalog)

        if not equipment_name:
            error = "Equipment type is required."
        elif not brand:
            error = "Brand is required."
        elif not equipment_location:
            error = "Equipment location is required."
        else:
            update_data = {
                "equipment_name": equipment_name,
                "serial_number": serial_number,
                "brand": brand,
                "equipment_location": equipment_location,
                "notes": equipment_notes,
                "parts": equipment_parts,
            }

            db.equipment.update_one({"_id": ObjectId(equipmentId), "customer_id": customerId}, {"$set": update_data})
            return redirect(url_for("customers.view_equipment", customerId=customerId, equipmentId=equipmentId))

    return render_template(
        "equipment/update_equipment.html",
        customerId=customerId,
        equipmentId=equipmentId,
        customer=serialize_doc(customer),
        equipment=serialize_doc(equipment),
        parts=part_docs,
        parts_catalog_json=json.dumps(part_catalog),
        error=error,
    )


@bp.route("/customers/<customerId>/equipment/<equipmentId>/delete", methods=["POST"])
def delete_equipment(customerId, equipmentId):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return redirect(url_for("customers.customers"))

    db.equipment.delete_one({"_id": object_id_or_404(equipmentId), "customer_id": customerId})
    return redirect(url_for("customers.view_customer", customerId=customerId))
