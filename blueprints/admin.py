from calendar import monthrange
from datetime import datetime

from bson import ObjectId
from flask import Blueprint, redirect, render_template, request, session, url_for

from mongo import ensure_connection_or_500
from utils.invoices import collect_invoice_items

bp = Blueprint("admin_bp", __name__)

def _coerce_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _format_date(value):
    if not value:
        return "-"

    if isinstance(value, datetime):
        return value.strftime("%m/%d/%Y")

    text_value = str(value).strip()
    if not text_value:
        return "-"

    try:
        normalized = text_value.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).strftime("%m/%d/%Y")
    except ValueError:
        return text_value


def _parse_datetime(value):
    if not value:
        return None

    if isinstance(value, datetime):
        return value

    text_value = str(value).strip()
    if not text_value:
        return None

    try:
        normalized = text_value.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _build_next_billing_date(start_date_value):
    start_date = _parse_datetime(start_date_value)
    if not start_date:
        return "-"

    today = datetime.utcnow()
    target_year = today.year
    target_month = today.month + 1
    if target_month == 13:
        target_month = 1
        target_year += 1

    billing_day = min(start_date.day, monthrange(target_year, target_month)[1])
    next_billing = datetime(target_year, target_month, billing_day)
    return next_billing.strftime("%m/%d/%Y")


def _get_current_employee(db):
    employee_id = session.get("employee_id")
    if not employee_id or not ObjectId.is_valid(employee_id):
        return None

    return db.employees.find_one({"_id": ObjectId(employee_id)})


def _get_subscription_document(db, employee):
    subscription_id = (employee.get("subscription_id") or "").strip()
    if not subscription_id:
        return None

    return db.subscriptions.find_one({"subscription_id": subscription_id})


def _build_subscription_view_model(db, employee, subscription_doc):
    subscription_id = (subscription_doc.get("subscription_id") or employee.get("subscription_id") or "").strip()
    add_ons = []
    for addon in subscription_doc.get("add_ons") or []:
        add_ons.append(
            {
                "id": (addon.get("add_on_id") or "").strip(),
                "name": addon.get("name") or "Add-On",
                "description": addon.get("description") or "",
                "price": _coerce_float(addon.get("price")),
            }
        )

    plan_price = _coerce_float(subscription_doc.get("price"))
    monthly_total = plan_price + sum(addon["price"] for addon in add_ons)
    active_employee_count = db.employees.count_documents({"subscription_id": subscription_id})

    is_cancelled = (subscription_doc.get("status") or "").lower() == "cancelled"
    next_billing_date = "-" if is_cancelled else _build_next_billing_date(subscription_doc.get("start_date"))
    ended_on = _format_date(subscription_doc.get("end_date")) if is_cancelled else None

    return {
        "subscription_id": subscription_id,
        "plan_name": subscription_doc.get("subscription_name") or "Subscription",
        "description": subscription_doc.get("description") or "",
        "plan_price": plan_price,
        "monthly_total": monthly_total,
        "status": (subscription_doc.get("status") or "").replace("_", " ").title() or "-",
        "billing_cycle": (subscription_doc.get("billing_cycle") or "").title() or "-",
        "started_on": _format_date(subscription_doc.get("start_date")),
        "next_billing_date": next_billing_date,
        "renews_on": next_billing_date,
        "active_employees": active_employee_count,
        "current_users": subscription_doc.get("current_users", active_employee_count),
        "max_active_employees": subscription_doc.get("max_users") or 0,
        "add_ons": add_ons,
        "is_cancelled": is_cancelled,
        "ended_on": ended_on,
    }


@bp.route("/admin")
def admin():
    return render_template("admin/admin.html")


@bp.route("/invoices")
def invoices():
    db = ensure_connection_or_500()
    invoice_items = collect_invoice_items(db)
    return render_template("invoices/invoices.html", invoices=invoice_items)


@bp.route("/admin/subscription")
def subscription():
    db = ensure_connection_or_500()
    employee = _get_current_employee(db)
    if not employee:
        return redirect(url_for("auth.login"))

    subscription_doc = _get_subscription_document(db, employee)
    subscription_data = _build_subscription_view_model(db, employee, subscription_doc) if subscription_doc else None

    return render_template(
        "admin/subscription.html",
        subscription=subscription_data,
        subscription_issue=(subscription_data is None),
        cancellation_message=(request.args.get("cancelled") == "1"),
    )


@bp.route("/admin/subscription/manage", methods=["GET", "POST"])
def manage_subscription():
    db = ensure_connection_or_500()
    employee = _get_current_employee(db)
    if not employee:
        return redirect(url_for("auth.login"))

    subscription_doc = _get_subscription_document(db, employee)
    if not subscription_doc:
        return render_template(
            "admin/manage_subscription.html",
            subscription=None,
            subscription_issue=True,
            updated=False,
        )

    if request.method == "POST":
        addon_id = request.form.get("addon_id", "").strip()
        if addon_id:
            db.subscriptions.update_one(
                {"subscription_id": subscription_doc.get("subscription_id")},
                {"$pull": {"add_ons": {"add_on_id": addon_id}}},
            )
        return redirect(url_for("admin_bp.manage_subscription", updated="1"))

    subscription_data = _build_subscription_view_model(db, employee, subscription_doc)
    return render_template(
        "admin/manage_subscription.html",
        subscription=subscription_data,
        subscription_issue=False,
        updated=(request.args.get("updated") == "1"),
    )


@bp.route("/admin/subscription/cancel", methods=["GET", "POST"])
def cancel_subscription():
    db = ensure_connection_or_500()
    employee = _get_current_employee(db)
    if not employee:
        return redirect(url_for("auth.login"))

    subscription_doc = _get_subscription_document(db, employee)
    if not subscription_doc:
        return render_template("admin/cancel_subscription.html", subscription_issue=True)

    if request.method == "POST":
        cancellation_reason = request.form.get("cancellation_reason", "").strip()
        db.subscriptions.update_one(
            {"subscription_id": subscription_doc.get("subscription_id")},
            {
                "$set": {
                    "status": "cancelled",
                    "reason_cancelled": cancellation_reason,
                    "end_date": datetime.utcnow().isoformat() + "Z",
                }
            },
        )
        return redirect(url_for("admin_bp.subscription", cancelled="1"))

    return render_template("admin/cancel_subscription.html", subscription_issue=False)


@bp.route("/admin/subscription/reactivate")
def reactivate_subscription():
    return render_template("admin/reactivate_subscription.html")
