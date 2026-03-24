from flask import Blueprint, redirect, render_template, request, url_for

from mongo import ensure_connection_or_500, object_id_or_404, serialize_doc
from utils.currency import normalize_currency
from utils.formatters import normalize_duration

bp = Blueprint("catalog", __name__)


@bp.route("/services")
def manage_services():
    db = ensure_connection_or_500()
    services = [serialize_doc(service) for service in db.services.find().sort("service_type", 1)]
    return render_template(
        "services/manage_services.html",
        services=services,
    )


@bp.route("/parts")
def manage_parts():
    db = ensure_connection_or_500()
    parts = [serialize_doc(part) for part in db.parts.find().sort("part_name", 1)]
    return render_template(
        "services/manage_parts.html",
        parts=parts,
    )


@bp.route("/parts/create", methods=["GET", "POST"])
def create_part():
    db = ensure_connection_or_500()
    if request.method == "POST":
        part_name = request.form.get("part_name", "").strip()
        part_default_price = normalize_currency(request.form.get("part_price", ""))

        if part_name:
            db.parts.insert_one(
                {
                    "part_name": part_name,
                    "part_default_price": part_default_price,
                }
            )

        return redirect(url_for("catalog.manage_parts"))

    return render_template("services/create_part.html")


@bp.route("/parts/<partId>")
def view_part(partId):
    db = ensure_connection_or_500()
    part = db.parts.find_one({"_id": object_id_or_404(partId)})
    if not part:
        return redirect(url_for("catalog.manage_parts"))

    return render_template(
        "services/view_part.html",
        partId=partId,
        part=serialize_doc(part),
    )


@bp.route("/services/create", methods=["GET", "POST"])
def create_service():
    db = ensure_connection_or_500()
    if request.method == "POST":
        service_name = request.form.get("job_type", "").strip()
        service_default_price = normalize_currency(request.form.get("job_price", ""))
        service_duration = normalize_duration(request.form.get("service_duration", ""))

        if service_name:
            db.services.insert_one(
                {
                    "service_type": service_name,
                    "service_default_price": service_default_price,
                    "service_duration": service_duration,
                }
            )

        return redirect(url_for("catalog.manage_services"))

    return render_template("services/create_service.html")


@bp.route("/services/<serviceId>")
def view_service(serviceId):
    db = ensure_connection_or_500()
    service = db.services.find_one({"_id": object_id_or_404(serviceId)})
    if not service:
        return redirect(url_for("catalog.manage_services"))

    return render_template(
        "services/view_service.html",
        serviceId=serviceId,
        service=serialize_doc(service),
    )


@bp.route("/services/<serviceId>/delete", methods=["POST"])
def delete_service(serviceId):
    db = ensure_connection_or_500()
    db.services.delete_one({"_id": object_id_or_404(serviceId)})
    return redirect(url_for("catalog.manage_services"))
