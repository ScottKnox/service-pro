from bson import ObjectId
from flask import Blueprint, redirect, render_template, request, session, url_for

from mongo import ensure_connection_or_500, serialize_doc

bp = Blueprint("business", __name__)


@bp.route("/business")
def business_profile():
    db = ensure_connection_or_500()

    employee_id = session.get("employee_id")
    employee = db.employees.find_one({"_id": ObjectId(employee_id)})
    if not employee:
        session.clear()
        return redirect(url_for("auth.login"))

    business_ref = employee.get("business")
    if not business_ref:
        return redirect(url_for("error_page", error="no_business"))

    business_oid = None
    if isinstance(business_ref, ObjectId):
        business_oid = business_ref
    elif isinstance(business_ref, str) and ObjectId.is_valid(business_ref):
        business_oid = ObjectId(business_ref)

    if not business_oid:
        return redirect(url_for("error_page", error="no_business"))

    business = db.businesses.find_one({"_id": business_oid})

    if not business:
        return redirect(url_for("error_page", error="no_business"))

    business = serialize_doc(business)

    return render_template("business/business_profile.html", business=business)


@bp.route("/business/update", methods=["GET", "POST"])
def update_business():
    db = ensure_connection_or_500()

    employee_id = session.get("employee_id")
    employee = db.employees.find_one({"_id": ObjectId(employee_id)})
    if not employee:
        session.clear()
        return redirect(url_for("auth.login"))

    business_ref = employee.get("business")
    if not business_ref:
        return redirect(url_for("error_page", error="no_business"))

    business_oid = None
    if isinstance(business_ref, ObjectId):
        business_oid = business_ref
    elif isinstance(business_ref, str) and ObjectId.is_valid(business_ref):
        business_oid = ObjectId(business_ref)

    if not business_oid:
        return redirect(url_for("error_page", error="no_business"))

    if request.method == "POST":
        company_name = request.form.get("company_name", "").strip()
        tax_rate = request.form.get("tax_rate", "0").strip()
        quote_email_template = request.form.get("quote_email_template", "").strip()
        invoice_email_template = request.form.get("invoice_email_template", "").strip()

        db.businesses.update_one(
            {"_id": business_oid},
            {
                "$set": {
                    "company_name": company_name,
                    "tax_rate": tax_rate,
                    "quote_email_template": quote_email_template,
                    "invoice_email_template": invoice_email_template,
                }
            },
        )

        return redirect(url_for("business.business_profile"))

    business = db.businesses.find_one({"_id": business_oid})
    if not business:
        return redirect(url_for("error_page", error="no_business"))

    business = serialize_doc(business)
    return render_template("business/update_business.html", business=business)
