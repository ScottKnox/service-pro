from flask import Blueprint, render_template

from mongo import ensure_connection_or_500
from utils.invoices import collect_invoice_items

bp = Blueprint("admin_bp", __name__)


@bp.route("/admin")
def admin():
    return render_template("admin/admin.html")


@bp.route("/invoices")
def invoices():
    db = ensure_connection_or_500()
    invoice_items = collect_invoice_items(db)
    return render_template("invoices/invoices.html", invoices=invoice_items)
