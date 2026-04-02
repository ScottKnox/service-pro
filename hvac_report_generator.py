from datetime import datetime
import os

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

# Section/field definitions for diagnostics report rendering.
# Each entry: (section_label, ((field_name, field_label), ...))
_REPORT_SECTIONS = (
    (
        "Airflow",
        (
            ("supplySideStaticPressure", "Supply Side Static Pressure"),
            ("returnSideStaticPressure", "Return Side Static Pressure"),
            ("totalExternalStaticPressure", "Total External Static Pressure"),
            ("correctStaticPressureForFanSpeed", "Correct Static Pressure for Fan Speed"),
            ("fanSpeed", "Fan Speed"),
            ("cfm", "CFM"),
            ("staticPressureNotes", "Static Pressure Notes"),
        ),
    ),
    (
        "Refrigerant",
        (
            ("refrigerantType", "Refrigerant Type"),
            ("outsideTemperature", "Outside Temperature"),
            ("temperatureDelta", "Temperature Delta"),
            ("correctTempuratureDelta", "Correct Temperature Delta"),
            ("lowSidePressure", "Low Side Pressure"),
            ("correctLowSidePressure", "Correct Low Side Pressure"),
            ("highSidePressure", "High Side Pressure"),
            ("correctHighSidePressure", "Correct High Side Pressure"),
            ("superheat", "Superheat"),
            ("correctSuperheat", "Correct Superheat"),
            ("subcooling", "Subcooling"),
            ("correctSubcooling", "Correct Subcooling"),
        ),
    ),
    (
        "Electrical",
        (
            ("highVoltageToDisconnect", "High Voltage to Disconnect"),
            ("correctHighVoltageToDisconnect", "Correct High Voltage to Disconnect"),
            ("contactorVoltage", "Contactor Voltage"),
            ("correctContactorVoltage", "Correct Contactor Voltage"),
            ("lowVoltageTransformerRToC", "Low Voltage Transformer R to C"),
            ("correctLowVoltageTransformerRToC", "Correct Low Voltage Transformer R to C"),
            ("wygToCThermostatVoltage", "W/Y/G to C Thermostat Voltage"),
            ("correctWygToCThermostateVoltage", "Correct W/Y/G to C Thermostat Voltage"),
            ("voltageDropCheck", "Voltage Drop Check"),
            ("voltageDropNotes", "Voltage Drop Notes"),
            ("blowerMotorVoltage", "Blower Motor Voltage"),
            ("correctBlowerMotorVoltage", "Correct Blower Motor Voltage"),
            ("fanMotorVoltage", "Fan Motor Voltage"),
            ("correctFanMotorVoltage", "Correct Fan Motor Voltage"),
            ("compressorVoltage", "Compressor Voltage"),
            ("correctCompressorVoltage", "Correct Compressor Voltage"),
            ("capacitorTerminalVoltage", "Capacitor Terminal Voltage"),
            ("correctCapacitorTerminalVoltage", "Correct Capacitor Terminal Voltage"),
        ),
    ),
)


def generate_hvac_system_health_report(hvac_system_id, customer, hvac_system, diagnostics_card, report_number, raw_diagnostics=None):
    """Generate a PDF health report for a single HVAC system diagnostics entry."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"hvac_report_{hvac_system_id}_{timestamp}.pdf"
    reports_dir = os.path.join(os.path.dirname(__file__), "invoices")
    os.makedirs(reports_dir, exist_ok=True)
    filepath = os.path.join(reports_dir, filename)

    doc = SimpleDocTemplate(filepath, pagesize=letter, topMargin=0.5 * inch, bottomMargin=0.5 * inch)
    styles = getSampleStyleSheet()
    story = []

    title_style = ParagraphStyle(
        "HvacReportTitle",
        parent=styles["Heading1"],
        fontSize=22,
        textColor=colors.HexColor("#1B263B"),
        spaceAfter=12,
        alignment=1,
    )

    section_heading_style = ParagraphStyle(
        "HvacReportSectionHeading",
        parent=styles["Heading2"],
        fontSize=12,
        textColor=colors.HexColor("#FFFFFF"),
        spaceAfter=0,
        spaceBefore=14,
    )

    footer_style = ParagraphStyle(
        "HvacReportFooter",
        parent=styles["Normal"],
        fontSize=10,
        textColor=colors.HexColor("#666666"),
        alignment=1,
    )

    customer_name = " ".join(
        part
        for part in [
            str(customer.get("first_name", "")).strip(),
            str(customer.get("last_name", "")).strip(),
        ]
        if part
    ).strip() or "Unknown Customer"

    story.append(Paragraph("SYSTEM HEALTH REPORT", title_style))
    story.append(Spacer(1, 0.2 * inch))

    report_info = [
        ["Report Number:", report_number],
        ["Date Generated:", datetime.now().strftime("%m/%d/%Y")],
        ["Customer:", customer_name],
        ["System Type:", str(hvac_system.get("system_type", "HVAC System"))],
        ["Location Type:", str(hvac_system.get("location_type", "Location not set"))],
        ["Date Performed:", str(diagnostics_card.get("date_performed", "-"))],
    ]
    report_info_table = Table(report_info, colWidths=[1.8 * inch, 4.3 * inch])
    report_info_table.setStyle(
        TableStyle(
            [
                ("FONT", (0, 0), (-1, -1), "Helvetica", 11),
                ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#1B263B")),
                ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    story.append(report_info_table)
    story.append(Spacer(1, 0.3 * inch))

    raw = raw_diagnostics if isinstance(raw_diagnostics, dict) else {}

    for section_label, fields in _REPORT_SECTIONS:
        # Build rows only for fields that have a value.
        rows = []
        for field_name, field_label in fields:
            raw_value = raw.get(field_name, "")
            value = str(raw_value).strip() if raw_value not in (None, "", []) else ""
            if value:
                rows.append([field_label, value])

        # Section heading rendered as a single-cell full-width table so we can
        # apply a dark background behind white text without needing Paragraph nesting.
        heading_row = Table(
            [[Paragraph(section_label, section_heading_style)]],
            colWidths=[6.1 * inch],
        )
        heading_row.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#1B263B")),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ]
            )
        )
        story.append(heading_row)

        if not rows:
            na_table = Table([["N/A", ""]], colWidths=[3.3 * inch, 2.8 * inch])
            na_table.setStyle(
                TableStyle(
                    [
                        ("FONT", (0, 0), (-1, -1), "Helvetica-Oblique", 10),
                        ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#888888")),
                        ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                        ("TOPPADDING", (0, 0), (-1, -1), 6),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                    ]
                )
            )
            story.append(na_table)
        else:
            data_table = Table([["Field", "Value"]] + rows, colWidths=[3.3 * inch, 2.8 * inch])
            row_count = len(rows) + 1
            data_table.setStyle(
                TableStyle(
                    [
                        ("FONT", (0, 0), (-1, -1), "Helvetica", 10),
                        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 10),
                        ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#1B263B")),
                        ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LINEBELOW", (0, 0), (-1, 0), 1, colors.HexColor("#1B263B")),
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E9EEF4")),
                        ("ROWBACKGROUNDS", (0, 1), (-1, row_count - 1), [colors.white, colors.HexColor("#F7F9FB")]),
                        ("TOPPADDING", (0, 0), (-1, -1), 5),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                    ]
                )
            )
            story.append(data_table)

        story.append(Spacer(1, 0.15 * inch))

    story.append(Spacer(1, 0.15 * inch))
    story.append(
        Paragraph(
            "This report reflects the latest saved diagnostics for the HVAC system.",
            footer_style,
        )
    )

    doc.build(story)
    return filepath
