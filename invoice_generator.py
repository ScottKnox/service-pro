from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib import colors
from datetime import datetime
import os


def generate_invoice(job_id, job, customer):
    """
    Generate a PDF invoice for a completed job.

    Args:
        job_id: The job ID
        job: Dictionary containing job data
        customer: Dictionary containing customer data

    Returns:
        The file path of the generated invoice PDF
    """
    # Create filename
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"invoice_job{job_id}_{timestamp}.pdf"
    filepath = os.path.join(os.path.dirname(__file__), "invoices", filename)

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

    # Title
    story.append(Paragraph("INVOICE", title_style))
    story.append(Spacer(1, 0.2 * inch))

    # Invoice header info
    invoice_info = [
        ["Invoice Number:", f"INV-{job_id}"],
        ["Invoice Date:", datetime.now().strftime("%m/%d/%Y")],
        ["Job ID:", str(job_id)],
    ]
    invoice_table = Table(invoice_info, colWidths=[1.5 * inch, 2 * inch])
    invoice_table.setStyle(
        TableStyle(
            [
                ("FONT", (0, 0), (-1, -1), "Helvetica", 11),
                ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#1B263B")),
                ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    story.append(invoice_table)
    story.append(Spacer(1, 0.3 * inch))

    # Customer and Job Details section
    story.append(Paragraph("Customer Information", heading_style))
    customer_info = [
        ["Name:", f"{customer.get('first_name', 'N/A')} {customer.get('last_name', 'N/A')}"],
        ["Company:", customer.get("company", "N/A")],
        ["Phone:", customer.get("phone", "N/A")],
        ["Email:", customer.get("email", "N/A")],
        ["Address:", f"{customer.get('address_line_1', 'N/A')}"],
        ["", f"{customer.get('city', 'N/A')}, {customer.get('state', 'N/A')}"],
    ]
    customer_table = Table(customer_info, colWidths=[1.2 * inch, 3.8 * inch])
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
    story.append(customer_table)
    story.append(Spacer(1, 0.3 * inch))

    # Service Details
    story.append(Paragraph("Service Details", heading_style))
    service_info = [
        ["Service Type:", job.get("job_type", "N/A")],
        ["Location:", f"{job.get('address_line_1', 'N/A')}"],
        ["", f"{job.get('city', 'N/A')}, {job.get('state', 'N/A')}"],
        ["Assigned Employee:", job.get("assigned_employee", "N/A")],
        ["Scheduled Date:", job.get("scheduled_date", "N/A")],
        ["Date Created:", job.get("date_created", "N/A")],
        ["Notes:", job.get("notes", "N/A")],
    ]
    service_table = Table(service_info, colWidths=[1.2 * inch, 3.8 * inch])
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
    story.append(service_table)
    story.append(Spacer(1, 0.4 * inch))

    # Amount Due
    amount_str = job.get("price", "$0.00").replace("$", "").replace(",", "")
    total_amount_data = [
        ["Total Amount Due:", job.get("price", "$0.00")],
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
                ("LINBELOW", (0, 0), (-1, -1), 2, colors.HexColor("#1B263B")),
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
            "Thank you for your business! <br/>This invoice was generated on " + datetime.now().strftime("%m/%d/%Y %H:%M:%S"),
            footer_style,
        )
    )

    # Build PDF
    doc.build(story)

    return filepath


def generate_quote(job_id, job, customer):
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

    # Customer and Job Details section
    story.append(Paragraph("Customer Information", heading_style))
    customer_info = [
        ["Name:", f"{customer.get('first_name', 'N/A')} {customer.get('last_name', 'N/A')}"],
        ["Company:", customer.get("company", "N/A")],
        ["Phone:", customer.get("phone", "N/A")],
        ["Email:", customer.get("email", "N/A")],
        ["Address:", f"{customer.get('address_line_1', 'N/A')}"],
        ["", f"{customer.get('city', 'N/A')}, {customer.get('state', 'N/A')}"],
    ]
    customer_table = Table(customer_info, colWidths=[1.2 * inch, 3.8 * inch])
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
    story.append(customer_table)
    story.append(Spacer(1, 0.3 * inch))

    # Service Details
    story.append(Paragraph("Service Details", heading_style))
    service_info = [
        ["Service Type:", job.get("job_type", "N/A")],
        ["Location:", f"{job.get('address_line_1', 'N/A')}"],
        ["", f"{job.get('city', 'N/A')}, {job.get('state', 'N/A')}"],
        ["Assigned Employee:", job.get("assigned_employee", "N/A")],
        ["Scheduled Date:", job.get("scheduled_date", "N/A")],
        ["Notes:", job.get("notes", "N/A")],
    ]
    service_table = Table(service_info, colWidths=[1.2 * inch, 3.8 * inch])
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
    story.append(service_table)
    story.append(Spacer(1, 0.4 * inch))

    # Estimated Amount
    total_amount_data = [
        ["Estimated Total:", job.get("price", "$0.00")],
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
