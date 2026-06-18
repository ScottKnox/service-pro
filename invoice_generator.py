from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image as ReportImage
from reportlab.lib import colors
from datetime import datetime, timedelta
from io import BytesIO
import os
import base64
from xml.sax.saxutils import escape as xml_escape

from PIL import Image as PILImage
from utils import object_storage
from utils.qr_codes import generate_payment_qr
from utils.taxes import build_line_item_tax_inputs, calculate_itemized_tax, normalize_business_tax_rates


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


def _is_effectively_zero(value):
    return abs(_currency_to_float(value)) < 0.000001


def _format_display_hours(value):
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        numeric = float(text)
    except ValueError:
        return text
    return str(int(numeric)) if numeric.is_integer() else f"{numeric:g}"


def _format_duration_with_unit(value):
    duration = _format_display_hours(value)
    if not duration:
        return ""

    try:
        numeric = float(duration)
    except ValueError:
        return duration

    unit = "hour" if numeric == 1 else "hours"
    return f"{duration} {unit}"


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


def _build_service_line_item_cell(name, description):
    title_text = str(name or "").strip() or "Service"
    description_text = str(description or "").strip()
    if not description_text:
        return title_text

    line_item_style = ParagraphStyle(
        "ServiceLineItemCell",
        fontName="Helvetica",
        fontSize=9,
        leading=11,
        textColor=colors.HexColor("#1B263B"),
    )
    escaped_title = xml_escape(title_text)
    escaped_description = xml_escape(description_text).replace("\n", "<br/>")
    markup = f"{escaped_title}<br/><font size='8' color='#5A6B82'>{escaped_description}</font>"
    return Paragraph(markup, line_item_style)


def _parse_mmddyyyy_date(date_string):
    raw_value = str(date_string or "").strip()
    if not raw_value:
        return None
    try:
        return datetime.strptime(raw_value, "%m/%d/%Y")
    except ValueError:
        return None


def _resolve_invoice_due_date(job):
    existing_due_date = str((job or {}).get("payment_due_date") or "").strip()
    if existing_due_date:
        return existing_due_date

    try:
        payment_due_days = int(str((job or {}).get("payment_due_days") or "").strip())
    except (TypeError, ValueError):
        payment_due_days = 30
    payment_due_days = max(1, payment_due_days)

    base_date = _parse_mmddyyyy_date((job or {}).get("scheduled_date"))
    if not base_date:
        base_date = _parse_mmddyyyy_date((job or {}).get("date_created"))
    if not base_date:
        base_date = datetime.now()

    return (base_date + timedelta(days=payment_due_days)).strftime("%m/%d/%Y")


def _build_section_table(story, heading_style, title, headers, rows, col_widths, section_spacing=0.22 * inch):
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
    story.append(Spacer(1, section_spacing))


def _resolve_logo_bytes(business_logo_path):
    raw_reference = str(business_logo_path or "").strip()
    if raw_reference:
        if os.path.exists(raw_reference):
            try:
                with open(raw_reference, "rb") as logo_fp:
                    logo_data = logo_fp.read()
                if logo_data:
                    return logo_data
            except Exception:
                pass

        remote_logo = object_storage.download_object_bytes(raw_reference)
        if remote_logo:
            return remote_logo

    default_logo_path = os.path.join(os.path.dirname(__file__), "logos", "company_logo.png")
    if os.path.exists(default_logo_path):
        try:
            with open(default_logo_path, "rb") as logo_fp:
                return logo_fp.read()
        except Exception:
            return b""

    return b""


def _append_logo(story, business_logo_path):
    logo_bytes = _resolve_logo_bytes(business_logo_path)
    if not logo_bytes:
        return

    try:
        logo_source = BytesIO(logo_bytes)
        with PILImage.open(logo_source) as logo_image:
            width, height = logo_image.size
        if not width or not height:
            return

        max_width = 2.5 * inch
        max_height = 1.0 * inch
        scale = min(max_width / width, max_height / height)

        report_logo_stream = BytesIO(logo_bytes)
        report_logo = ReportImage(report_logo_stream, width=width * scale, height=height * scale)
        report_logo.hAlign = "CENTER"
        report_logo._logo_stream = report_logo_stream
        story.append(report_logo)
        story.append(Spacer(1, 0.12 * inch))
    except Exception:
        return


def _build_logo_flowable(business_logo_path, max_width=2.5 * inch, max_height=1.0 * inch):
    logo_bytes = _resolve_logo_bytes(business_logo_path)
    if not logo_bytes:
        return None

    try:
        logo_source = BytesIO(logo_bytes)
        with PILImage.open(logo_source) as logo_image:
            width, height = logo_image.size
        if not width or not height:
            return None

        scale = min(max_width / width, max_height / height)
        logo_stream = BytesIO(logo_bytes)
        logo = ReportImage(logo_stream, width=width * scale, height=height * scale)
        logo.hAlign = "LEFT"
        logo._logo_stream = logo_stream
        return logo
    except Exception:
        return None


def _line_item_count(job):
    total = 0
    for key in ("services", "parts", "materials", "equipments"):
        total += sum(1 for item in (job.get(key) or []) if isinstance(item, dict))
    return total


def _service_display_name(service):
    return (
        service.get("name")
        or service.get("service_name")
        or service.get("type")
        or "Service"
    )


def _service_display_price(service):
    return (
        service.get("standard_price")
        or service.get("price")
        or "$0.00"
    )


def _service_display_duration(service):
    return _format_duration_with_unit(
        service.get("labor_hours")
        or service.get("estimated_hours")
        or service.get("duration")
        or ""
    )


def _part_display_name(part):
    return (
        part.get("name")
        or part.get("part_name")
        or part.get("description")
        or "Part"
    )


def _part_display_amount(part):
    return (
        part.get("price")
        or part.get("unit_cost")
        or part.get("sell_price")
        or "$0.00"
    )


def _build_payment_qr_flowable(payment_url, qr_size=80):
    try:
        qr_code_b64 = generate_payment_qr(payment_url)
        if not qr_code_b64:
            return None
        qr_bytes = base64.b64decode(qr_code_b64)
        qr_stream = BytesIO(qr_bytes)
        qr_flowable = ReportImage(qr_stream, width=qr_size, height=qr_size)
        qr_flowable.hAlign = "RIGHT"
        qr_flowable._qr_stream = qr_stream
        return qr_flowable
    except Exception:
        return None


def generate_invoice(job_id, job, customer, business_logo_path="", business=None, payment_url=""):
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
    compact_layout = _line_item_count(job) <= 4
    header_spacer_height = 0.14 * inch if compact_layout else 0.2 * inch
    info_spacer_height = 0.16 * inch if compact_layout else 0.24 * inch
    section_spacer_height = 0.12 * inch if compact_layout else 0.22 * inch
    line_items_to_totals_spacer_height = 0.16 * inch if compact_layout else 0.26 * inch
    totals_spacer_height = 0.08 * inch if compact_layout else 0.14 * inch

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
        or "Klovent"
    )

    invoice_number = f"INV-{str(job_id)[:8].upper()}"
    payment_due_date = _resolve_invoice_due_date(job)

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

    contact_column_widths = [1.7 * inch, 2.15 * inch]
    left_contact_table = Table(left_contact_rows or [[Paragraph("", info_value_style)]], colWidths=[contact_column_widths[0]])
    right_contact_table = Table(right_contact_rows or [[Paragraph("", info_value_style)]], colWidths=[contact_column_widths[1]])
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

    business_contact_table = Table([[left_contact_table, right_contact_table]], colWidths=contact_column_widths)
    business_contact_table.setStyle(
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
    business_contact_rows.append([business_contact_table])

    left_header_block = Table(business_contact_rows, colWidths=[3.9 * inch])
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
        [Paragraph("Due Date", info_label_style), Paragraph(payment_due_date, info_value_style)],
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
        colWidths=[2.7 * inch],
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
        colWidths=[3.9 * inch, 2.7 * inch],
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
    story.append(Spacer(1, header_spacer_height))

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
            [Paragraph(f"Proposed Job Date: {str(job.get('scheduled_date') or 'N/A')}", info_value_style)],
            [Paragraph(f"Proposed Job Time: {_format_time_to_am_pm(job.get('scheduled_time'))}", info_value_style)],
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
    story.append(Spacer(1, info_spacer_height))
    story.append(Paragraph("Description of Work Performed and Materials", work_heading_style))

    service_rows = []
    for service in job.get("services", []) or []:
        if not isinstance(service, dict):
            continue
        name = _service_display_name(service)
        price = _service_display_price(service)
        description_lines = []
        base_description = str(service.get("description") or "").strip()
        if base_description:
            description_lines.append(base_description)
        if service.get("show_labor_breakdown"):
            labor_hours = str(service.get("labor_hours") or "").strip()
            if labor_hours and labor_hours not in {"0", "0.0"}:
                description_lines.append(
                    f"Labor {labor_hours} hrs @ {str(service.get('labor_rate') or '$0.00')} /hr = {str(service.get('labor_total') or '$0.00')}"
                )
            for item in service.get("service_parts") or []:
                if not isinstance(item, dict):
                    continue
                part_name = str(item.get("part_name") or "Part").strip()
                quantity = str(item.get("quantity") or "").strip()
                unit_cost = str(item.get("unit_cost") or "").strip()
                line = part_name
                if quantity:
                    line += f" {quantity}"
                if unit_cost:
                    line += f" @ {unit_cost}"
                description_lines.append(line)
            for item in service.get("service_materials") or []:
                if not isinstance(item, dict):
                    continue
                material_name = str(item.get("material_name") or "Material").strip()
                quantity = str(item.get("default_quantity_used") or item.get("quantity") or "").strip()
                unit = str(item.get("unit_of_measure") or "").strip()
                unit_price = str(item.get("price") or item.get("unit_price") or "").strip()
                line = material_name
                if quantity:
                    line += f" {quantity}"
                if unit:
                    line += f" {unit}"
                if unit_price:
                    line += f" @ {unit_price}"
                description_lines.append(line)
        description = "\n".join(description_lines)
        service_rows.append([_build_service_line_item_cell(name, description), str(price)])

    parts_rows = []
    for part in job.get("parts", []) or []:
        if not isinstance(part, dict):
            continue
        amount = _part_display_amount(part)
        if _is_effectively_zero(amount):
            continue
        parts_rows.append([
            str(_part_display_name(part)),
            str(amount),
        ])

    material_rows = []
    for material in job.get("materials", []) or []:
        if not isinstance(material, dict):
            continue
        amount = material.get("line_total") or "$0.00"
        if _is_effectively_zero(amount):
            continue
        qty = _format_display_hours(material.get("quantity_used") or "0")
        unit = str(material.get("unit_of_measure") or "").strip()
        qty_display = f"{qty} {unit}".strip()
        material_rows.append([
            str(material.get("material_name") or "Material"),
            qty_display or "0",
            str(amount),
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

    _build_section_table(story, heading_style, "", ["Service", "Amount"], service_rows, [4.4 * inch, 2.2 * inch], section_spacing=section_spacer_height)
    _build_section_table(story, heading_style, "", ["Part", "Amount"], parts_rows, [4.4 * inch, 2.2 * inch], section_spacing=section_spacer_height)
    _build_section_table(story, heading_style, "", ["Material", "Quantity", "Amount"], material_rows, [3.2 * inch, 1.2 * inch, 2.2 * inch], section_spacing=section_spacer_height)
    _build_section_table(story, heading_style, "", ["Equipment", "Qty", "Price", "Amount"], equipment_rows, [2.6 * inch, 0.8 * inch, 1.2 * inch, 2.0 * inch], section_spacing=section_spacer_height)

    subtotal = (
        sum(_currency_to_float(row[1]) for row in service_rows)
        + sum(_currency_to_float(row[1]) for row in parts_rows)
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
        is_plan_savings = str(discount.get("source") or "").strip() == "maintenance_plan"

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
            # The maintenance plan savings row is already named for the customer;
            # show it verbatim instead of appending the generic "Discount (...)".
            label = discount_name if is_plan_savings else f"{discount_name} Discount ({display_value})"
            discount_rows_for_totals.append([
                label,
                _format_currency(-amount_value),
            ])

    discounts_total = min(discounts_total, subtotal)
    base_total = subtotal - discounts_total

    tax_inputs = build_line_item_tax_inputs(job)
    if subtotal > 0 and discounts_total > 0:
        discount_ratio = base_total / subtotal
        tax_inputs = [{**item, "amount": item["amount"] * discount_ratio} for item in tax_inputs]
    tax_breakdown = calculate_itemized_tax(
        tax_inputs,
        normalize_business_tax_rates(business),
        customer_tax_exempt=bool((customer or {}).get("tax_exempt")),
    )
    tax_total = float(tax_breakdown.get("tax_total") or 0.0)
    final_total = base_total + tax_total

    summary_rows = [
        ["Subtotal", _format_currency(subtotal)],
    ]
    if discount_rows_for_totals:
        summary_rows.extend(discount_rows_for_totals)
    elif discounts_total > 0:
        summary_rows.append(["Discount", _format_currency(-discounts_total)])
    if tax_breakdown.get("is_tax_exempt"):
        summary_rows.append(["Tax exempt", "$0.00"])
    else:
        for tax_line in tax_breakdown.get("tax_lines") or []:
            summary_rows.append([str(tax_line.get("display_name") or tax_line.get("name") or "Tax"), _format_currency(tax_line.get("amount"))])
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

    invoice_note_text = str(job.get("invoice_notes") or "").strip()
    notes_panel = None
    if invoice_note_text:
        notes_rows = [[Paragraph("Invoice Notes", section_panel_heading_style)]]
        notes_rows.append([Paragraph(invoice_note_text, info_value_style)])

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

    totals_panel = Table([[Spacer(1, 0.01 * inch), totals_table]], colWidths=[2.8 * inch, 3.8 * inch])
    totals_panel.setStyle(
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

    payment_panel = None
    payment_link = str(payment_url or "").strip()
    if payment_link:
        payment_qr_flowable = _build_payment_qr_flowable(payment_link, qr_size=80)
        if payment_qr_flowable:
            payment_label_style = ParagraphStyle(
                "InvoicePaymentLabel",
                parent=info_label_style,
                fontSize=9,
                textColor=colors.HexColor("#1B263B"),
                fontName="Helvetica-Bold",
                leading=11,
            )
            payment_link_style = ParagraphStyle(
                "InvoicePaymentLink",
                parent=info_value_style,
                fontSize=8,
                textColor=colors.HexColor("#4A5568"),
                leading=10,
            )
            payment_link_display = payment_link.replace("https://", "").replace("http://", "")
            payment_text_block = [
                [Paragraph("Scan to pay:", payment_label_style)],
                [Paragraph(xml_escape(payment_link_display), payment_link_style)],
            ]
            payment_text_table = Table(payment_text_block, colWidths=[2.8 * inch])
            payment_text_table.setStyle(
                TableStyle(
                    [
                        ("LEFTPADDING", (0, 0), (-1, -1), 0),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                        ("TOPPADDING", (0, 0), (-1, -1), 0),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ]
                )
            )

            payment_panel = Table(
                [[payment_text_table, payment_qr_flowable]],
                colWidths=[2.9 * inch, 0.95 * inch],
            )
            payment_panel.setStyle(
                TableStyle(
                    [
                        ("LEFTPADDING", (0, 0), (-1, -1), 0),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                        ("TOPPADDING", (0, 0), (-1, -1), 2),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ]
                )
            )

    footer_paragraph = Paragraph(
        "Thank you for your business!<br/>Please retain this invoice for your records.",
        footer_style,
    )

    story.append(Spacer(1, line_items_to_totals_spacer_height))

    story.append(totals_panel)
    if payment_panel:
        story.append(Spacer(1, 0.08 * inch))
        story.append(payment_panel)
    if notes_panel or warranty_panel:
        story.append(Spacer(1, 0.08 * inch))
        notes_warranty_row = Table(
            [[notes_panel or Spacer(1, 0.01 * inch), warranty_panel or Spacer(1, 0.01 * inch)]],
            colWidths=[3.3 * inch, 3.3 * inch],
        )
        notes_warranty_row.setStyle(
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
        story.append(notes_warranty_row)

    story.append(Spacer(1, totals_spacer_height))
    story.append(footer_paragraph)
    doc.build(story)

    return filepath


def generate_quote(job_id, job, customer, business_logo_path="", business=None):
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
    business = business or {}

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

    info_label_style = ParagraphStyle(
        "QuoteInfoLabel",
        parent=styles["Normal"],
        fontSize=8,
        textColor=colors.HexColor("#5A6B82"),
        fontName="Helvetica-Bold",
        spaceAfter=2,
        spaceBefore=1,
        leading=9,
    )

    info_value_style = ParagraphStyle(
        "QuoteInfoValue",
        parent=styles["Normal"],
        fontSize=10,
        textColor=colors.HexColor("#1B263B"),
        fontName="Helvetica",
        leading=12,
    )

    section_panel_heading_style = ParagraphStyle(
        "QuoteSectionPanelHeading",
        parent=heading_style,
        fontSize=11,
        spaceAfter=4,
    )

    normal_style = ParagraphStyle(
        "CustomNormal",
        parent=styles["Normal"],
        fontSize=11,
        textColor=colors.HexColor("#1B263B"),
    )

    company_name = (
        str(business.get("company_name") or "").strip()
        or str(business.get("business_name") or "").strip()
        or "Klovent"
    )

    expiration_days_raw = str(job.get("estimate_expiration_days") or "").strip()
    try:
        expiration_days = max(1, int(expiration_days_raw))
    except ValueError:
        expiration_days = 30

    valid_until_date = (datetime.now() + timedelta(days=expiration_days)).strftime("%m/%d/%Y")

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

    contact_column_widths = [1.7 * inch, 2.15 * inch]
    left_contact_table = Table(left_contact_rows or [[Paragraph("", info_value_style)]], colWidths=[contact_column_widths[0]])
    right_contact_table = Table(right_contact_rows or [[Paragraph("", info_value_style)]], colWidths=[contact_column_widths[1]])
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

    business_contact_table = Table([[left_contact_table, right_contact_table]], colWidths=contact_column_widths)
    business_contact_table.setStyle(
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
    business_contact_rows.append([business_contact_table])

    left_header_block = Table(business_contact_rows, colWidths=[3.9 * inch])
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

    quote_heading_style = ParagraphStyle(
        "QuoteHeading",
        parent=title_style,
        alignment=2,
        spaceAfter=18,
        fontSize=21,
    )

    # Quote header info
    quote_info = [
        [Paragraph("Quote Date", info_label_style), Paragraph(datetime.now().strftime("%m/%d/%Y"), info_value_style)],
        [Paragraph("Valid Until", info_label_style), Paragraph(valid_until_date, info_value_style)],
    ]
    quote_table = Table(quote_info, colWidths=[1.3 * inch, 1.8 * inch])
    quote_table.setStyle(
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
        [[Paragraph("ESTIMATE", quote_heading_style)], [Spacer(1, 0.08 * inch)], [quote_table]],
        colWidths=[2.7 * inch],
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

    header_table = Table([[left_header_block, right_header_block]], colWidths=[3.9 * inch, 2.7 * inch])
    header_table.setStyle(
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

    story.append(header_table)
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
    schedule_type = "recurring" if str(job.get("job_schedule_type") or "").strip() == "recurring" else "one_time"
    recurrence_summary = str(job.get("recurrence_summary") or "").strip()
    recurring_end_type = str(job.get("recurring_end_type") or "never").strip() or "never"
    recurring_end_label = {
        "never": "Never",
        "on_date": "On Date",
        "after_occurrences": "After Number of Visits",
    }.get(recurring_end_type, "Never")

    service_info = [
        ["Location:", f"{job.get('address_line_1', 'N/A')}"],
        ["", f"{job.get('city', 'N/A')}, {job.get('state', 'N/A')}"],
        ["Assigned Employee:", job.get("assigned_employee", "N/A")],
        ["Proposed Job Date:", job.get("scheduled_date", "N/A")],
        ["Proposed Job Time:", _format_time_to_am_pm(job.get("scheduled_time"))],
        ["Job Type:", "Recurring" if schedule_type == "recurring" else "One-Time"],
    ]
    if schedule_type == "recurring":
        service_info.append(["Recurrence:", recurrence_summary or "Recurring"])
        service_info.append(["Recurrence Ends:", recurring_end_label])
        if recurring_end_type == "on_date":
            service_info.append(["Recurrence End Date:", str(job.get("recurring_end_date") or "N/A")])
        elif recurring_end_type == "after_occurrences":
            service_info.append(["Number of Visits:", str(job.get("recurring_end_after") or "N/A")])

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
            if not isinstance(service, dict):
                continue
            name = (
                _service_display_name(service)
            )
            price = _service_display_price(service)
            duration = _service_display_duration(service)
            description = service.get("description") or service.get("service_description") or ""
            services_data.append([
                _build_service_line_item_cell(name, description),
                str(price),
                str(duration),
            ])
    
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
        parts_data = [["Part", "Price"]]
        for part in job.get('parts', []):
            if not isinstance(part, dict):
                continue
            amount = _part_display_amount(part)
            if _is_effectively_zero(amount):
                continue
            parts_data.append([str(_part_display_name(part)), str(amount)])

        if len(parts_data) > 1:
            story.append(Paragraph("Parts", heading_style))
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

    if job.get('materials'):
        materials_data = [["Material", "Quantity", "Price", "Line Total"]]
        for material in job.get('materials', []):
            if not (isinstance(material, dict) and material.get('material_name')):
                continue
            line_total = material.get('line_total', '$0.00')
            if _is_effectively_zero(line_total):
                continue
            materials_data.append([
                material.get('material_name', ''),
                f"{material.get('quantity_used', '')} {material.get('unit_of_measure', '')}".strip(),
                material.get('price', '$0.00'),
                line_total,
            ])

        if len(materials_data) > 1:
            story.append(Paragraph("Materials", heading_style))
            materials_breakdown_table = Table(materials_data, colWidths=[2.2 * inch, 1.0 * inch, 1.0 * inch, 1.3 * inch])
            materials_breakdown_table.setStyle(
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
            story.append(materials_breakdown_table)
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

    services_list = job.get("services") if isinstance(job.get("services"), list) else []
    parts_list = job.get("parts") if isinstance(job.get("parts"), list) else []
    materials_list = job.get("materials") if isinstance(job.get("materials"), list) else []
    equipments_list = job.get("equipments") if isinstance(job.get("equipments"), list) else []

    subtotal = (
        sum(_currency_to_float(_service_display_price(service)) for service in services_list)
        + sum(_currency_to_float(_part_display_amount(part)) for part in parts_list)
        + sum(_currency_to_float(material.get("line_total") or material.get("price")) for material in materials_list)
        + sum(_currency_to_float(equipment.get("line_total") or equipment.get("price")) for equipment in equipments_list)
    )

    discounts_total = 0.0
    for discount in (job.get("discounts") or []):
        if not isinstance(discount, dict):
            continue
        percent = _currency_to_float(discount.get("discount_percentage"))
        if percent > 0:
            discounts_total += max(0.0, subtotal * (percent / 100.0))
            continue
        discounts_total += abs(_currency_to_float(discount.get("discount_amount") or discount.get("line_total")))

    discounts_total = min(discounts_total, max(0.0, subtotal))
    pre_tax_total = max(0.0, subtotal - discounts_total)
    tax_breakdown = calculate_itemized_tax(
        build_line_item_tax_inputs(job),
        normalize_business_tax_rates(business),
        customer_tax_exempt=bool((customer or {}).get("tax_exempt")),
    )
    tax_total = float(tax_breakdown.get("tax_total") or 0.0)
    estimate_total = pre_tax_total + tax_total

    total_amount_data = [["Subtotal:", _format_currency(subtotal)]]
    if discounts_total > 0:
        total_amount_data.append(["Discounts:", _format_currency(-discounts_total)])
    if tax_breakdown.get("is_tax_exempt"):
        total_amount_data.append(["Tax exempt:", "$0.00"])
    else:
        for tax_line in tax_breakdown.get("tax_lines") or []:
            total_amount_data.append([
                str(tax_line.get("display_name") or tax_line.get("name") or "Tax") + ":",
                _format_currency(tax_line.get("amount")),
            ])
    total_amount_data.append(["Estimated Total:", _format_currency(estimate_total)])

    total_table = Table(total_amount_data, colWidths=[3 * inch, 1.7 * inch])
    total_table.setStyle(
        TableStyle(
            [
                ("FONT", (0, 0), (-1, -1), "Helvetica", 11),
                ("FONT", (0, -1), (-1, -1), "Helvetica-Bold", 12),
                ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#1B263B")),
                ("ALIGN", (0, 0), (0, -1), "RIGHT"),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("LINEABOVE", (0, -1), (-1, -1), 2, colors.HexColor("#1B263B")),
            ]
        )
    )
    story.append(total_table)
    story.append(Spacer(1, 0.3 * inch))

    estimate_note_text = str(job.get("estimate_notes") or "").strip()
    if not estimate_note_text:
        first_note = ""
        for note in (job.get("notes") or []):
            if isinstance(note, dict):
                first_note = str(note.get("text") or "").strip()
                if first_note:
                    break
        estimate_note_text = first_note

    notes_panel = None
    if estimate_note_text:
        notes_rows = [[Paragraph("Estimate Notes", section_panel_heading_style)]]
        notes_rows.append([Paragraph(estimate_note_text, normal_style)])
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

    business_doc = business or {}
    warranty_info = str(business_doc.get("warranty_info") or "").strip()
    warranty_panel = None
    if warranty_info:
        warranty_rows = [[Paragraph("Warranty Info", section_panel_heading_style)]]
        for line in warranty_info.splitlines():
            clean_line = str(line).strip()
            if clean_line:
                warranty_rows.append([Paragraph(clean_line, normal_style)])
        if len(warranty_rows) == 1:
            warranty_rows.append([Paragraph(warranty_info, normal_style)])

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

    if notes_panel or warranty_panel:
        notes_warranty_row = Table(
            [[notes_panel or Spacer(1, 0.01 * inch), warranty_panel or Spacer(1, 0.01 * inch)]],
            colWidths=[2.8 * inch, 3.8 * inch],
        )
        notes_warranty_row.setStyle(
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
        story.append(notes_warranty_row)
        story.append(Spacer(1, 0.18 * inch))

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


def generate_estimate(estimate_id, estimate, customer, business_logo_path="", business=None):
    """Generate a PDF estimate from an estimate document.

    This reuses the quote layout generator by adapting estimate keys to the
    expected quote payload shape.
    """
    estimate_payload = dict(estimate or {})
    estimate_payload.setdefault("assigned_employee", estimate_payload.get("estimated_by_employee", ""))
    if "estimate_notes" in estimate_payload and not estimate_payload.get("notes"):
        note_text = str(estimate_payload.get("estimate_notes") or "").strip()
        estimate_payload["notes"] = [{"text": note_text}] if note_text else []

    return generate_quote(
        estimate_id,
        estimate_payload,
        customer,
        business_logo_path=business_logo_path,
        business=business,
    )
