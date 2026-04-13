from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image as ReportImage
from reportlab.lib import colors
from datetime import datetime
import os

from PIL import Image as PILImage


def _currency_to_float(value):
    text = str(value or "").strip()
    if not text:
        return 0.0

    negative = False
    if text.startswith("-"):
        negative = True
        text = text[1:]

    text = text.replace("$", "").replace(",", "").strip()
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1].strip()

    try:
        amount = float(text)
    except ValueError:
        return 0.0

    return -amount if negative else amount


def _format_currency(value):
    amount = float(value or 0)
    sign = "-" if amount < 0 else ""
    return f"{sign}${abs(amount):,.2f}"


def _format_display_hours(value):
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        numeric = float(text)
    except ValueError:
        return text
    return str(int(numeric)) if numeric.is_integer() else f"{numeric:g}"


def _format_time_to_am_pm(time_string):
    text = str(time_string or "").strip()
    if not text:
        return "N/A"

    parts = text.split(":")
    if len(parts) < 2:
        return text

    try:
        hours24 = int(parts[0])
        minutes = int(parts[1])
    except ValueError:
        return text

    if hours24 < 0 or hours24 > 23 or minutes < 0 or minutes > 59:
        return text

    period = "PM" if hours24 >= 12 else "AM"
    hours12 = hours24 % 12 or 12
    return f"{hours12}:{minutes:02d} {period}"


def _build_section_table(story, heading_style, title, headers, rows, col_widths):
    if not rows:
        return

    if str(title or "").strip():
        story.append(Paragraph(title, heading_style))
    table_data = [headers] + rows
    section_table = Table(table_data, colWidths=col_widths)
    section_table.setStyle(
        TableStyle(
            [
                ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 10),
                ("FONT", (0, 1), (-1, -1), "Helvetica", 9),
                ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#1B263B")),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EEF2F7")),
                ("LINEBELOW", (0, 0), (-1, 0), 1, colors.HexColor("#8A97AB")),
                ("LINEBELOW", (0, 1), (-1, -1), 0.25, colors.HexColor("#D1D8E2")),
                ("ALIGN", (0, 0), (0, -1), "LEFT"),
                ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    story.append(section_table)
    story.append(Spacer(1, 0.22 * inch))


def _resolve_logo_path(business_logo_path):
    if business_logo_path and os.path.exists(business_logo_path):
        return business_logo_path

    default_logo_path = os.path.join(os.path.dirname(__file__), "logos", "company_logo.png")
    if os.path.exists(default_logo_path):
        return default_logo_path

    return ""


def _append_logo(story, business_logo_path):
    logo_path = _resolve_logo_path(business_logo_path)
    if not logo_path:
        return

    try:
        with PILImage.open(logo_path) as logo_image:
            width, height = logo_image.size
        if not width or not height:
            return

        max_width = 2.5 * inch
        max_height = 1.0 * inch
        scale = min(max_width / width, max_height / height)

        report_logo = ReportImage(logo_path, width=width * scale, height=height * scale)
        report_logo.hAlign = "CENTER"
        story.append(report_logo)
        story.append(Spacer(1, 0.12 * inch))
    except Exception:
        return


def _build_logo_flowable(business_logo_path, max_width=2.5 * inch, max_height=1.0 * inch):
    logo_path = _resolve_logo_path(business_logo_path)
    if not logo_path:
        return None

    try:
        with PILImage.open(logo_path) as logo_image:
            width, height = logo_image.size
        if not width or not height:
            return None

        scale = min(max_width / width, max_height / height)
        logo = ReportImage(logo_path, width=width * scale, height=height * scale)
        logo.hAlign = "LEFT"
        return logo
    except Exception:
        return None


def generate_invoice(job_id, job, customer, business_logo_path="", business=None):
    """
    Generate a PDF invoice for a completed job.

    Args:
        job_id: The job ID
        job: Dictionary containing job data
        customer: Dictionary containing customer data

    Returns:
        The file path of the generated invoice PDF
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"invoice_job{job_id}_{timestamp}.pdf"
    invoices_dir = os.path.join(os.path.dirname(__file__), "invoices")
    os.makedirs(invoices_dir, exist_ok=True)
    filepath = os.path.join(invoices_dir, filename)

    doc = SimpleDocTemplate(filepath, pagesize=letter, topMargin=0.5 * inch, bottomMargin=0.5 * inch)
    story = []
    styles = getSampleStyleSheet()
    business = business or {}

    title_style = ParagraphStyle(
        "CustomTitle",
        parent=styles["Heading1"],
        fontSize=22,
        textColor=colors.HexColor("#1B263B"),
        spaceAfter=8,
        alignment=0,
        fontName="Helvetica-Bold",
    )

    heading_style = ParagraphStyle(
        "CustomHeading",
        parent=styles["Heading2"],
        fontSize=12,
        textColor=colors.HexColor("#1B263B"),
        spaceAfter=6,
        fontName="Helvetica-Bold",
    )

    info_label_style = ParagraphStyle(
        "InfoLabel",
        parent=styles["Normal"],
        fontSize=8,
        textColor=colors.HexColor("#5A6B82"),
        fontName="Helvetica-Bold",
        spaceAfter=2,
        spaceBefore=1,
        leading=9,
    )

    info_value_style = ParagraphStyle(
        "InfoValue",
        parent=styles["Normal"],
        fontSize=10,
        textColor=colors.HexColor("#1B263B"),
        fontName="Helvetica",
        leading=12,
    )

    footer_style = ParagraphStyle(
        "Footer",
        parent=styles["Normal"],
        fontSize=9,
        textColor=colors.HexColor("#5A6B82"),
        alignment=1,
    )

    section_panel_heading_style = ParagraphStyle(
        "SectionPanelHeading",
        parent=heading_style,
        fontSize=11,
        spaceAfter=4,
    )

    company_name = (
        str(business.get("company_name") or "").strip()
        or str(business.get("business_name") or "").strip()
        or "Steady Work"
    )

    invoice_number = f"INV-{str(job_id)[:8].upper()}"

    logo_flowable = _build_logo_flowable(business_logo_path)
    business_contact_rows = []
    if logo_flowable:
        business_contact_rows.append([logo_flowable])
        business_contact_rows.append([Spacer(1, 0.03 * inch)])
    else:
        business_contact_rows.append([Paragraph(company_name, heading_style)])

    address_line_1 = str(business.get("address_line_1") or "").strip()
    address_line_2 = str(business.get("address_line_2") or "").strip()
    city = str(business.get("city") or "").strip()
    state = str(business.get("state") or "").strip()
    zip_code = str(business.get("zip_code") or "").strip()
    phone_number = str(business.get("phone_number") or "").strip()
    fax_number = str(business.get("fax_number") or "").strip()
    business_email = str(business.get("email") or "").strip()
    website = str(business.get("website") or "").strip()
    license_number = str(business.get("license_number") or "").strip()

    left_contact_rows = []
    right_contact_rows = []

    location_parts = [part for part in [city, state] if part]
    location_line = ", ".join(location_parts)
    if zip_code:
        location_line = f"{location_line} {zip_code}".strip()

    if address_line_1:
        left_contact_rows.append([Paragraph(address_line_1, info_value_style)])
    if address_line_2:
        left_contact_rows.append([Paragraph(address_line_2, info_value_style)])
    if location_line:
        left_contact_rows.append([Paragraph(location_line, info_value_style)])
    if left_contact_rows:
        left_contact_rows.append([Spacer(1, 0.08 * inch)])

    if phone_number:
        left_contact_rows.append([Paragraph(f"Phone: {phone_number}", info_value_style)])
        left_contact_rows.append([Spacer(1, 0.06 * inch)])
    if fax_number:
        left_contact_rows.append([Paragraph(f"Fax: {fax_number}", info_value_style)])
    if license_number:
        left_contact_rows.append([Spacer(1, 0.03 * inch)])
        left_contact_rows.append([Paragraph(f"License #: {license_number}", info_value_style)])

    if business_email:
        right_contact_rows.append([Paragraph(business_email, info_value_style)])
        right_contact_rows.append([Spacer(1, 0.06 * inch)])
    if website:
        right_contact_rows.append([Paragraph(website, info_value_style)])
        right_contact_rows.append([Spacer(1, 0.06 * inch)])

    if not left_contact_rows and not right_contact_rows:
        left_contact_rows.append([Paragraph(company_name, info_value_style)])

    left_contact_table = Table(left_contact_rows or [[Paragraph("", info_value_style)]], colWidths=[1.55 * inch])
    right_contact_table = Table(right_contact_rows or [[Paragraph("", info_value_style)]], colWidths=[1.55 * inch])
    for contact_table in (left_contact_table, right_contact_table):
        contact_table.setStyle(
            TableStyle(
                [
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]
            )
        )

    business_contact_rows.append([Table([[left_contact_table, right_contact_table]], colWidths=[1.58 * inch, 1.58 * inch])])

    left_header_block = Table(business_contact_rows, colWidths=[3.2 * inch])
    left_header_block.setStyle(
        TableStyle(
            [
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )

    invoice_heading_style = ParagraphStyle(
        "InvoiceHeading",
        parent=title_style,
        alignment=2,
        spaceAfter=22,
    )

    work_heading_style = ParagraphStyle(
        "WorkHeading",
        parent=heading_style,
        alignment=1,
        fontSize=11,
        spaceAfter=10,
    )

    invoice_info_rows = [
        [Paragraph("Invoice #", info_label_style), Paragraph(invoice_number, info_value_style)],
        [Paragraph("Invoice Date", info_label_style), Paragraph(datetime.now().strftime("%m/%d/%Y"), info_value_style)],
        [Paragraph("Due Date", info_label_style), Paragraph("TBD", info_value_style)],
    ]
    invoice_info_table = Table(invoice_info_rows, colWidths=[1.3 * inch, 1.8 * inch])
    invoice_info_table.setStyle(
        TableStyle(
            [
                ("LEFTPADDING", (0, 0), (-1, -1), 2),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 1),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
                ("ALIGN", (0, 0), (0, -1), "RIGHT"),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )

    right_header_block = Table(
        [[Paragraph("INVOICE", invoice_heading_style)], [Spacer(1, 0.08 * inch)], [invoice_info_table]],
        colWidths=[3.2 * inch],
    )
    right_header_block.setStyle(
        TableStyle(
            [
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )

    header_table = Table(
        [[left_header_block, right_header_block]],
        colWidths=[3.4 * inch, 3.2 * inch],
    )
    header_table.setStyle(
        TableStyle(
            [
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LINEBELOW", (0, 0), (-1, -1), 0.8, colors.HexColor("#D1D8E2")),
            ]
        )
    )
    story.append(header_table)
    story.append(Spacer(1, 0.2 * inch))

    customer_full_name = f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip() or str(job.get("customer_name") or "N/A")
    customer_company = str(customer.get("company") or job.get("company") or "N/A")
    customer_phone = str(customer.get("phone") or "N/A")
    billing_line_1 = str(customer.get("address_line_1") or job.get("address_line_1") or "N/A")
    billing_line_2 = str(customer.get("address_line_2") or "").strip()
    billing_city = str(customer.get("city") or job.get("city") or "").strip()
    billing_state = str(customer.get("state") or job.get("state") or "").strip()

    service_line_1 = str(job.get("address_line_1") or "N/A")
    service_line_2 = str(job.get("address_line_2") or "").strip()
    service_city = str(job.get("city") or "").strip()
    service_state = str(job.get("state") or "").strip()

    customer_rows = [
        [Paragraph("Bill To", heading_style)],
        [Paragraph(customer_full_name, info_value_style)],
        [Paragraph(customer_company, info_value_style)],
        [Paragraph(customer_phone, info_value_style)],
        [Paragraph(billing_line_1, info_value_style)],
    ]
    if billing_line_2:
        customer_rows.append([Paragraph(billing_line_2, info_value_style)])
    if billing_city or billing_state:
        customer_rows.append([Paragraph(f"{billing_city}, {billing_state}".strip(", "), info_value_style)])

    service_rows = [
        [Paragraph("Service Location", heading_style)],
        [Paragraph(service_line_1, info_value_style)],
    ]
    if service_line_2:
        service_rows.append([Paragraph(service_line_2, info_value_style)])
    if service_city or service_state:
        service_rows.append([Paragraph(f"{service_city}, {service_state}".strip(", "), info_value_style)])
    service_rows.extend(
        [
            [Paragraph(f"Scheduled Date: {str(job.get('scheduled_date') or 'N/A')}", info_value_style)],
            [Paragraph(f"Scheduled Time: {_format_time_to_am_pm(job.get('scheduled_time'))}", info_value_style)],
            [Paragraph(f"Assigned Employee: {str(job.get('assigned_employee') or 'N/A')}", info_value_style)],
        ]
    )

    customer_block = Table(customer_rows, colWidths=[3.1 * inch])
    service_block = Table(service_rows, colWidths=[3.1 * inch])
    for block in (customer_block, service_block):
        block.setStyle(
            TableStyle(
                [
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ]
            )
        )

    info_boxes = Table([[customer_block, service_block]], colWidths=[3.3 * inch, 3.3 * inch])
    info_boxes.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 1, colors.HexColor("#A8B1BD")),
                ("INNERGRID", (0, 0), (-1, -1), 1, colors.HexColor("#A8B1BD")),
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F8FAFC")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    story.append(info_boxes)
    story.append(Spacer(1, 0.24 * inch))
    story.append(Paragraph("Description of Work Performed and Materials", work_heading_style))

    service_rows = []
    for service in job.get("services", []) or []:
        if not isinstance(service, dict):
            continue
        name = service.get("name") or service.get("type") or service.get("code") or "Service"
        price = service.get("standard_price") or service.get("price") or "$0.00"
        service_rows.append([str(name), str(price)])

    parts_rows = []
    for part in job.get("parts", []) or []:
        if not isinstance(part, dict):
            continue
        parts_rows.append([
            str(part.get("name") or part.get("code") or "Part"),
            str(part.get("price") or part.get("unit_cost") or "$0.00"),
        ])

    labor_rows = []
    for labor in job.get("labors", []) or []:
        if not isinstance(labor, dict):
            continue
        labor_rows.append([
            str(labor.get("description") or "Labor"),
            _format_display_hours(labor.get("hours") or "0"),
            str(labor.get("hourly_rate") or "$0.00"),
            str(labor.get("line_total") or "$0.00"),
        ])

    material_rows = []
    for material in job.get("materials", []) or []:
        if not isinstance(material, dict):
            continue
        qty = _format_display_hours(material.get("quantity_used") or "0")
        unit = str(material.get("unit_of_measure") or "").strip()
        qty_display = f"{qty} {unit}".strip()
        material_rows.append([
            str(material.get("material_name") or "Material"),
            qty_display or "0",
            str(material.get("line_total") or "$0.00"),
        ])

    equipment_rows = []
    for equipment in job.get("equipments", []) or []:
        if not isinstance(equipment, dict):
            continue
        equipment_rows.append([
            str(equipment.get("equipment_name") or "Equipment"),
            _format_display_hours(equipment.get("quantity_installed") or "0"),
            str(equipment.get("price") or "$0.00"),
            str(equipment.get("line_total") or "$0.00"),
        ])

    _build_section_table(story, heading_style, "", ["Service", "Amount"], service_rows, [4.4 * inch, 2.2 * inch])
    _build_section_table(story, heading_style, "", ["Part", "Amount"], parts_rows, [4.4 * inch, 2.2 * inch])
    _build_section_table(story, heading_style, "", ["Description", "Hours", "Rate", "Amount"], labor_rows, [2.8 * inch, 0.8 * inch, 1.2 * inch, 1.8 * inch])
    _build_section_table(story, heading_style, "", ["Material", "Quantity", "Amount"], material_rows, [3.2 * inch, 1.2 * inch, 2.2 * inch])
    _build_section_table(story, heading_style, "", ["Equipment", "Qty", "Price", "Amount"], equipment_rows, [2.6 * inch, 0.8 * inch, 1.2 * inch, 2.0 * inch])

    subtotal = (
        sum(_currency_to_float(row[1]) for row in service_rows)
        + sum(_currency_to_float(row[1]) for row in parts_rows)
        + sum(_currency_to_float(row[3]) for row in labor_rows)
        + sum(_currency_to_float(row[2]) for row in material_rows)
        + sum(_currency_to_float(row[3]) for row in equipment_rows)
    )
    discount_rows_for_totals = []
    discounts_total = 0.0
    for discount in job.get("discounts", []) or []:
        if not isinstance(discount, dict):
            continue

        percent_raw = str(discount.get("discount_percentage") or "").strip()
        amount_raw = str(discount.get("discount_amount") or "").strip()

        percent_value = _currency_to_float(percent_raw)
        if percent_value > 0:
            percent_discount_amount = subtotal * (percent_value / 100.0)
            discounts_total += percent_discount_amount
            discount_name = str(discount.get("discount_name") or "Discount")
            discount_rows_for_totals.append([
                f"{discount_name} Discount ({percent_value:g}%)",
                _format_currency(-percent_discount_amount),
            ])
            continue

        amount_value = abs(_currency_to_float(amount_raw))
        if amount_value > 0:
            discounts_total += amount_value
            discount_name = str(discount.get("discount_name") or "Discount")
            display_value = amount_raw if "$" in amount_raw else _format_currency(amount_value)
            discount_rows_for_totals.append([
                f"{discount_name} Discount ({display_value})",
                _format_currency(-amount_value),
            ])

    discounts_total = min(discounts_total, subtotal)
    base_total = subtotal - discounts_total

    parts_total = sum(_currency_to_float(row[1]) for row in parts_rows)
    labor_total = sum(_currency_to_float(row[3]) for row in labor_rows)
    materials_total = sum(_currency_to_float(row[2]) for row in material_rows)
    installation_total = sum(_currency_to_float(row[1]) for row in service_rows) + sum(_currency_to_float(row[3]) for row in equipment_rows)
    fabrication_total = 0.0

    def _tax_line(enabled_key, rate_key, taxable_amount, label):
        enabled = str(business.get(enabled_key, "no")).strip().lower() == "yes"
        if not enabled:
            return None
        rate = float(str(business.get(rate_key, "0") or "0").strip() or "0")
        if rate <= 0 or taxable_amount <= 0:
            return None
        amount = taxable_amount * (rate / 100.0)
        return [label, _format_currency(amount)]

    tax_rows = [
        _tax_line("tax_parts", "tax_parts_rate", parts_total, f"Tax (Parts {business.get('tax_parts_rate', '0')}%)"),
        _tax_line("tax_repair_labor", "tax_repair_labor_rate", labor_total, f"Tax (Repair Labor {business.get('tax_repair_labor_rate', '0')}%)"),
        _tax_line("tax_materials", "tax_materials_rate", materials_total, f"Tax (Materials {business.get('tax_materials_rate', '0')}%)"),
        _tax_line("tax_installation", "tax_installation_rate", installation_total, f"Tax (Installation {business.get('tax_installation_rate', '0')}%)"),
        _tax_line("tax_fabrication", "tax_fabrication_rate", fabrication_total, f"Tax (Fabrication {business.get('tax_fabrication_rate', '0')}%)"),
    ]
    tax_rows = [row for row in tax_rows if row]
    tax_total = sum(_currency_to_float(row[1]) for row in tax_rows)

    computed_total = base_total + tax_total
    final_total = computed_total
    effective_tax_rate = (tax_total / base_total * 100.0) if base_total > 0 else 0.0

    summary_rows = [
        ["Subtotal", _format_currency(subtotal)],
    ]
    if discount_rows_for_totals:
        summary_rows.extend(discount_rows_for_totals)
    elif discounts_total > 0:
        summary_rows.append(["Discount", _format_currency(-discounts_total)])
    if tax_rows:
        summary_rows.extend(tax_rows)
    summary_rows.append([f"Tax Total ({effective_tax_rate:.2f}% Applied)", _format_currency(tax_total)])
    summary_rows.append(["Total Due", _format_currency(final_total)])

    totals_table = Table(summary_rows, colWidths=[2.4 * inch, 1.6 * inch])
    totals_table.setStyle(
        TableStyle(
            [
                ("FONT", (0, 0), (-1, -1), "Helvetica", 10),
                ("FONT", (0, -1), (-1, -1), "Helvetica-Bold", 12),
                ("ALIGN", (0, 0), (0, -1), "RIGHT"),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#1B263B")),
                ("LINEABOVE", (0, -1), (-1, -1), 1.6, colors.HexColor("#1B263B")),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )

    notes = [note for note in (job.get("notes") or []) if isinstance(note, dict) and str(note.get("text") or "").strip()]
    notes_panel = None
    if notes:
        notes_rows = [[Paragraph("Notes", section_panel_heading_style)]]
        for note in notes[-4:]:
            note_text = str(note.get("text") or "").strip()
            notes_rows.append([Paragraph(note_text, info_value_style)])

        notes_panel = Table(notes_rows, colWidths=[2.8 * inch])
        notes_panel.setStyle(
            TableStyle(
                [
                    ("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.HexColor("#A8B1BD")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 7),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]
            )
        )

    warranty_info = str(business.get("warranty_info") or "").strip()
    warranty_panel = None
    if warranty_info:
        warranty_rows = [[Paragraph("Warranty Info", section_panel_heading_style)]]
        for line in warranty_info.splitlines():
            clean_line = str(line).strip()
            if clean_line:
                warranty_rows.append([Paragraph(clean_line, info_value_style)])

        if len(warranty_rows) == 1:
            warranty_rows.append([Paragraph(warranty_info, info_value_style)])

        warranty_panel = Table(warranty_rows, colWidths=[3.8 * inch])
        warranty_panel.setStyle(
            TableStyle(
                [
                    ("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.HexColor("#A8B1BD")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 7),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]
            )
        )

    right_column_rows = [[totals_table]]
    if warranty_panel:
        right_column_rows.append([Spacer(1, 0.08 * inch)])
        right_column_rows.append([warranty_panel])

    right_column = Table(right_column_rows, colWidths=[3.8 * inch])
    right_column.setStyle(
        TableStyle(
            [
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )

    left_column_content = notes_panel if notes_panel else Spacer(1, 0.1 * inch)
    totals_wrap = Table([[left_column_content, right_column]], colWidths=[2.8 * inch, 3.8 * inch])
    totals_wrap.setStyle(
        TableStyle(
            [
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    story.append(totals_wrap)
    story.append(Spacer(1, 0.14 * inch))

    story.append(
        Paragraph(
            "Thank you for your business!<br/>Please retain this invoice for your records.",
            footer_style,
        )
    )
    doc.build(story)

    return filepath


def generate_quote(job_id, job, customer, business_logo_path=""):
    """
    Generate a PDF quote for a job in Estimate status.

    Args:
        job_id: The job ID
        job: Dictionary containing job data
        customer: Dictionary containing customer data

    Returns:
        The file path of the generated quote PDF
    """
    # Create filename
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"quote_job{job_id}_{timestamp}.pdf"
    quotes_dir = os.path.join(os.path.dirname(__file__), "invoices")  # Store in invoices dir
    os.makedirs(quotes_dir, exist_ok=True)
    filepath = os.path.join(quotes_dir, filename)

    # Create PDF document
    doc = SimpleDocTemplate(filepath, pagesize=letter, topMargin=0.5 * inch, bottomMargin=0.5 * inch)
    story = []
    styles = getSampleStyleSheet()

    # Custom styles
    title_style = ParagraphStyle(
        "CustomTitle",
        parent=styles["Heading1"],
        fontSize=24,
        textColor=colors.HexColor("#1B263B"),
        spaceAfter=12,
        alignment=1,  # Center alignment
    )

    heading_style = ParagraphStyle(
        "CustomHeading",
        parent=styles["Heading2"],
        fontSize=14,
        textColor=colors.HexColor("#1B263B"),
        spaceAfter=12,
    )

    normal_style = ParagraphStyle(
        "CustomNormal",
        parent=styles["Normal"],
        fontSize=11,
        textColor=colors.HexColor("#1B263B"),
    )

    _append_logo(story, business_logo_path)

    # Title
    story.append(Paragraph("ESTIMATE / QUOTE", title_style))
    story.append(Spacer(1, 0.2 * inch))

    # Quote header info
    quote_info = [
        ["Quote Date:", datetime.now().strftime("%m/%d/%Y")],
        ["Job ID:", str(job_id)],
        ["Valid Until:", (datetime.now() + __import__('datetime').timedelta(days=30)).strftime("%m/%d/%Y")],
    ]
    quote_table = Table(quote_info, colWidths=[1.5 * inch, 2 * inch])
    quote_table.setStyle(
        TableStyle(
            [
                ("FONT", (0, 0), (-1, -1), "Helvetica", 11),
                ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#1B263B")),
                ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    story.append(quote_table)
    story.append(Spacer(1, 0.3 * inch))

    # Customer Information and Service Details - Two Column Layout
    # Left column - Customer Information
    customer_info = [
        ["Name:", f"{customer.get('first_name', 'N/A')} {customer.get('last_name', 'N/A')}"],
        ["Company:", customer.get("company", "N/A")],
        ["Phone:", customer.get("phone", "N/A")],
        ["Email:", customer.get("email", "N/A")],
        ["Address:", f"{customer.get('address_line_1', 'N/A')}"],
        ["", f"{customer.get('city', 'N/A')}, {customer.get('state', 'N/A')}"],
    ]
    customer_table = Table(customer_info, colWidths=[0.9 * inch, 2.3 * inch])
    customer_table.setStyle(
        TableStyle(
            [
                ("FONT", (0, 0), (-1, -1), "Helvetica", 10),
                ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#1B263B")),
                ("ALIGN", (0, 0), (0, -1), "RIGHT"),
                ("ALIGN", (1, 0), (1, -1), "LEFT"),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )

    # Right column - Service Details
    service_info = [
        ["Location:", f"{job.get('address_line_1', 'N/A')}"],
        ["", f"{job.get('city', 'N/A')}, {job.get('state', 'N/A')}"],
        ["Assigned Employee:", job.get("assigned_employee", "N/A")],
        ["Scheduled Date:", job.get("scheduled_date", "N/A")],
        ["Scheduled Time:", job.get("scheduled_time", "N/A")],
    ]
    service_table = Table(service_info, colWidths=[0.9 * inch, 2.3 * inch])
    service_table.setStyle(
        TableStyle(
            [
                ("FONT", (0, 0), (-1, -1), "Helvetica", 10),
                ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#1B263B")),
                ("ALIGN", (0, 0), (0, -1), "RIGHT"),
                ("ALIGN", (1, 0), (1, -1), "LEFT"),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )

    # Two-column wrapper table
    two_column_data = [
        [
            Paragraph("Customer Information", heading_style),
            Paragraph("Service Details", heading_style),
        ],
        [customer_table, service_table],
    ]
    two_column_table = Table(two_column_data, colWidths=[3.3 * inch, 3.3 * inch])
    two_column_table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    story.append(two_column_table)
    story.append(Spacer(1, 0.3 * inch))

    # Services Breakdown Table
    story.append(Paragraph("Services", heading_style))
    services_data = [["Service", "Price", "Duration"]]
    if job.get('services'):
        for service in job.get('services', []):
            if isinstance(service, dict) and 'type' in service and 'price' in service:
                services_data.append([service['type'], service['price'], service.get('duration', '')])
    
    services_breakdown_table = Table(services_data, colWidths=[2.8 * inch, 1.2 * inch, 1.5 * inch])
    services_breakdown_table.setStyle(
        TableStyle(
            [
                ("FONT", (0, 0), (-1, -1), "Helvetica", 10),
                ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 10),
                ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#1B263B")),
                ("ALIGN", (0, 0), (0, -1), "LEFT"),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("ALIGN", (2, 0), (2, -1), "LEFT"),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LINEBELOW", (0, 0), (-1, 0), 1, colors.HexColor("#1B263B")),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E9EEF4")),
            ]
        )
    )
    story.append(services_breakdown_table)
    story.append(Spacer(1, 0.4 * inch))

    if job.get('parts'):
        story.append(Paragraph("Parts", heading_style))
        parts_data = [["Part", "Price"]]
        for part in job.get('parts', []):
            if isinstance(part, dict) and 'name' in part and 'price' in part:
                parts_data.append([part['name'], part['price']])

        parts_breakdown_table = Table(parts_data, colWidths=[4.0 * inch, 1.5 * inch])
        parts_breakdown_table.setStyle(
            TableStyle(
                [
                    ("FONT", (0, 0), (-1, -1), "Helvetica", 10),
                    ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 10),
                    ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#1B263B")),
                    ("ALIGN", (0, 0), (0, -1), "LEFT"),
                    ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LINEBELOW", (0, 0), (-1, 0), 1, colors.HexColor("#1B263B")),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E9EEF4")),
                ]
            )
        )
        story.append(parts_breakdown_table)
        story.append(Spacer(1, 0.4 * inch))

    if job.get('equipments'):
        story.append(Paragraph("Equipment", heading_style))
        equipment_data = [["Equipment", "Qty", "Price", "Line Total"]]
        for equipment in job.get('equipments', []):
            if isinstance(equipment, dict) and equipment.get('equipment_name'):
                equipment_data.append([
                    equipment.get('equipment_name', ''),
                    equipment.get('quantity_installed', ''),
                    equipment.get('price', '$0.00'),
                    equipment.get('line_total', '$0.00'),
                ])

        equipment_breakdown_table = Table(equipment_data, colWidths=[2.2 * inch, 0.8 * inch, 1.2 * inch, 1.3 * inch])
        equipment_breakdown_table.setStyle(
            TableStyle(
                [
                    ("FONT", (0, 0), (-1, -1), "Helvetica", 10),
                    ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 10),
                    ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#1B263B")),
                    ("ALIGN", (0, 0), (0, -1), "LEFT"),
                    ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LINEBELOW", (0, 0), (-1, 0), 1, colors.HexColor("#1B263B")),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E9EEF4")),
                ]
            )
        )
        story.append(equipment_breakdown_table)
        story.append(Spacer(1, 0.4 * inch))

    # Estimated Amount
    total_amount_data = [
        ["Estimated Total:", job.get("total", "$0.00")],
    ]
    total_table = Table(total_amount_data, colWidths=[3 * inch, 1 * inch])
    total_table.setStyle(
        TableStyle(
            [
                ("FONT", (0, 0), (-1, -1), "Helvetica-Bold", 12),
                ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#1B263B")),
                ("ALIGN", (0, 0), (0, -1), "RIGHT"),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("LINEABOVE", (0, 0), (-1, -1), 2, colors.HexColor("#1B263B")),
                ("LINEBELOW", (0, 0), (-1, -1), 2, colors.HexColor("#1B263B")),
            ]
        )
    )
    story.append(total_table)
    story.append(Spacer(1, 0.3 * inch))

    # Footer
    footer_style = ParagraphStyle(
        "Footer",
        parent=styles["Normal"],
        fontSize=10,
        textColor=colors.HexColor("#666666"),
        alignment=1,  # Center
    )
    story.append(
        Paragraph(
            "Please contact us if you have any questions about this estimate. <br/>This quote was generated on " + datetime.now().strftime("%m/%d/%Y %H:%M:%S"),
            footer_style,
        )
    )

    # Build PDF
    doc.build(story)

    return filepath
