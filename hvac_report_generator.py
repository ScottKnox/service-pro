"""
HVAC Diagnostic Report Generator
Produces a customer-friendly multi-page PDF using ReportLab.
Designed to match the Knox Diagnostic Report layout.
"""
from datetime import datetime
from io import BytesIO
import os
import re

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    HRFlowable,
    Image as ReportImage,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from PIL import Image, ImageOps

# ---------------------------------------------------------------------------
# Field/section definitions
# ---------------------------------------------------------------------------

_REPORT_SECTIONS = (
    (
        "Airflow and duct system",
        "How well air moves through your home.",
        (
            ("supplyStaticPressure", "Supply static pressure", "in WC"),
            ("returnStaticPressure", "Return static pressure", "in WC"),
            ("totalExternalStaticPressure", "Total external static", "in WC"),
            ("designStaticPressure", "Design static pressure", "in WC"),
            ("temperatureDelta", "Temperature delta", "°F"),
            ("wellInsulated", "Duct insulation", None),
            ("wellSupported", "Duct supports", None),
            ("dampersFunctioningProperly", "Dampers", None),
            ("damaged", "Duct damage", None),
            ("leaks", "Leaks detected", None),
            ("staticPressureNotes", "Technician notes", None),
        ),
    ),
    (
        "Refrigerant circuit",
        "The fluid that moves heat out of your home.",
        (
            ("refrigerantType", "Refrigerant type", None),
            ("suctionPressure", "Suction pressure", "PSIG"),
            ("dischargePressure", "Discharge pressure", "PSIG"),
            ("superheat", "Superheat", "°F"),
            ("targetSuperheat", "Target superheat", "°F"),
            ("subcooling", "Subcooling", "°F"),
            ("targetSubcooling", "Target subcooling", "°F"),
            ("leaks", "Leaks detected", None),
            ("notesOnRefrigerant", "Technician notes", None),
        ),
    ),
    (
        "Electrical system",
        "Power supply, safety, and component health.",
        (
            ("outdoorDisconnectVoltage", "Disconnect voltage", "V"),
            ("compressorAmperage", "Compressor amperage", "A"),
            ("targetCompressorAmperage", "Target amperage", "A"),
            ("acCapacitorVoltage", "AC capacitor voltage", "V"),
            ("targetAcCapacitorVoltage", "Target capacitor voltage", "V"),
            ("lowVoltage24V", "Low voltage 24V", None),
            ("groundWirePresent", "Ground wire", None),
            ("contactorCondition", "Contactor condition", None),
            ("electricalNotes", "Technician notes", None),
        ),
    ),
    (
        "Indoor air quality",
        "What you and your family are breathing.",
        (
            ("relativeHumidity", "Relative humidity", "%"),
            ("carbonMonoxide", "Carbon monoxide", "PPM"),
            ("carbonDioxide", "Carbon dioxide", "PPM"),
            ("vocLevels", "VOC levels", "µg/m³"),
            ("pm25", "PM2.5 particulates", "µg/m³"),
            ("pm10", "PM10 particulates", "µg/m³"),
            ("moldOrMildew", "Mold or mildew", None),
        ),
    ),
)

_NUMERIC_PATTERN = re.compile(r"[-+]?\d*\.?\d+")

_REPORT_SECTION_PHOTO_KEYS = {
    "Airflow and duct system": ("airflow", "ductwork"),
    "Refrigerant circuit": ("refrigerant",),
    "Electrical system": ("electrical",),
    "Indoor air quality": ("indoor_air_quality",),
}


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _safe(value, fallback="-"):
    text = str(value or "").strip()
    return text if text else fallback


def _normalize_section_key(value):
    key_text = str(value or "").strip().lower()
    if not key_text:
        return ""
    return re.sub(r"[^a-z0-9]+", "_", key_text).strip("_")


def _resolve_photo_absolute_path(photo):
    if not isinstance(photo, dict):
        return ""

    filename = str(photo.get("filename") or "").strip()
    if filename:
        candidate = os.path.abspath(os.path.join(os.path.dirname(__file__), "static", "uploads", "hvac_photos", filename))
        if os.path.exists(candidate):
            return candidate

    raw_url = str(photo.get("url") or "").strip()
    if raw_url.startswith("/static/"):
        relative_path = raw_url[len("/static/"):]
        candidate = os.path.abspath(os.path.join(os.path.dirname(__file__), "static", relative_path.replace("/", os.sep)))
        if os.path.exists(candidate):
            return candidate

    return ""


def _section_photo_entries_for_report(raw_diagnostics, report_section_label):
    if not isinstance(raw_diagnostics, dict):
        return []

    section_photos = raw_diagnostics.get("section_photos")
    if not isinstance(section_photos, dict):
        return []

    requested_keys = _REPORT_SECTION_PHOTO_KEYS.get(report_section_label, (_normalize_section_key(report_section_label),))
    normalized_photo_map = {
        _normalize_section_key(section_key): photos
        for section_key, photos in section_photos.items()
        if isinstance(photos, list)
    }

    merged_entries = []
    for requested_key in requested_keys:
        normalized_key = _normalize_section_key(requested_key)
        if not normalized_key:
            continue
        for photo in normalized_photo_map.get(normalized_key, []):
            if isinstance(photo, dict):
                merged_entries.append(photo)

    return merged_entries


def _build_pdf_photo_flowable(photo_path, max_width, max_height):
    try:
        with Image.open(photo_path) as source_image:
            normalized_image = ImageOps.exif_transpose(source_image)
            if normalized_image.mode not in ("RGB", "L"):
                normalized_image = normalized_image.convert("RGB")
            elif normalized_image.mode == "L":
                normalized_image = normalized_image.convert("RGB")

            normalized_image.thumbnail((1800, 1800), Image.Resampling.LANCZOS)

            encoded = BytesIO()
            normalized_image.save(encoded, format="JPEG", quality=80, optimize=True)
            encoded.seek(0)

        reader = ImageReader(encoded)
        width_px, height_px = reader.getSize()
        if not width_px or not height_px:
            return None

        scale_ratio = min(max_width / float(width_px), max_height / float(height_px), 1.0)
        flowable = ReportImage(
            encoded,
            width=float(width_px) * scale_ratio,
            height=float(height_px) * scale_ratio,
        )
        flowable.hAlign = "LEFT"
        flowable._image_buffer = encoded
        return flowable
    except Exception:
        try:
            reader = ImageReader(photo_path)
            width_px, height_px = reader.getSize()
            if not width_px or not height_px:
                return None
            scale_ratio = min(max_width / float(width_px), max_height / float(height_px), 1.0)
            flowable = ReportImage(
                photo_path,
                width=float(width_px) * scale_ratio,
                height=float(height_px) * scale_ratio,
            )
            flowable.hAlign = "LEFT"
            return flowable
        except Exception:
            return None


def _parse_float(value):
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip().replace(",", "")
    m = _NUMERIC_PATTERN.search(text)
    return float(m.group(0)) if m else None


def _format_value(field_name, raw_value, unit_override=None):
    """Return a display string for a raw field value, appending units and humanising booleans."""
    text = _safe(raw_value)
    if text == "-":
        return "-"

    _bool_map = {
        "yes": "Yes", "no": "No", "true": "Yes", "false": "No",
        "present": "Present", "good": "Good",
        "well insulated": "Well insulated",
        "questionable": "Questionable â€” monitor",
        "broken": "Broken needs repair",
        "flex improperly positioned": "Flex improperly positioned",
        "functioning properly": "Functioning properly",
        "none": "None",
        "none detected": "None detected",
    }
    lower = text.lower()
    if lower in _bool_map:
        return _bool_map[lower]

    if unit_override:
        if unit_override.lower().replace("°", "") not in lower.replace("°", ""):
            return f"{text} {unit_override}"
    return text


# ---------------------------------------------------------------------------
# Scoring logic
# ---------------------------------------------------------------------------

def _compute_health_scores(values, raw):
    """
    Returns a dict:
        overall, airflow, refrigerant, electrical, ductwork, air_quality  (each 0-100)
    """
    def _cond(key):
        return _safe(values.get(key), "No Data")

    scores = {}

    # Airflow
    temp_cond = _cond("temperatureDeltaOverallCondition")
    sp_cond = _cond("staticPressureOverallCondition")
    airflow_pts = 50 if temp_cond == "Within Spec" else (20 if temp_cond in ("Low", "High") else 30)
    total_sp = _parse_float(raw.get("totalExternalStaticPressure"))
    design_sp = _parse_float(raw.get("designStaticPressure"))
    if total_sp is not None and design_sp is not None and design_sp > 0:
        ratio = total_sp / design_sp
        airflow_pts += 50 if 0.85 <= ratio <= 1.15 else (25 if 0.6 <= ratio <= 1.4 else 5)
    elif sp_cond == "Within Spec":
        airflow_pts += 50
    elif sp_cond in ("Low", "High"):
        airflow_pts += 20
    else:
        airflow_pts += 30
    scores["airflow"] = min(100, max(0, airflow_pts))

    # Ductwork
    duct_score = 100
    if _safe(raw.get("leaks"), "").lower() not in ("no", "none", ""):
        duct_score -= 15
    if _safe(raw.get("wellSupported"), "").lower() in ("no", "broken", "false"):
        duct_score -= 20
    if _safe(raw.get("damaged"), "").lower() in ("yes", "true"):
        duct_score -= 20
    if _safe(raw.get("wellInsulated"), "").lower() in ("no", "false"):
        duct_score -= 15
    if _safe(raw.get("dampersFunctioningProperly"), "").lower() in ("no", "false"):
        duct_score -= 10
    scores["ductwork"] = max(0, duct_score)

    # Refrigerant
    ref_cond = _cond("superheatSubcoolingOverallCondition")
    ref_score = 100 if ref_cond == "Within Spec" else (50 if ref_cond in ("Low", "High") else 75)
    if _safe(raw.get("leaks"), "").lower() not in ("no", "none", ""):
        ref_score = min(ref_score, 60)
    scores["refrigerant"] = ref_score

    # Electrical
    comp_cond = _cond("compressorAmperageOverallCondition")
    cap_cond = _cond("acCapacitorOverallCondition")
    elec_score = 100
    if comp_cond == "High":
        elec_score -= 25
    elif comp_cond == "Low":
        elec_score -= 15
    if cap_cond in ("High", "Low"):
        elec_score -= 20
    contactor = _safe(raw.get("contactorCondition"), "").lower()
    if "questionable" in contactor or "warn" in contactor or "wear" in contactor or "monitor" in contactor:
        elec_score -= 20
    elif "replace" in contactor or "failed" in contactor or "bad" in contactor:
        elec_score -= 35
    scores["electrical"] = max(0, elec_score)

    # Air quality
    co_cond = _cond("carbonMonoxideOverallCondition")
    aq_score = 100
    if co_cond == "High":
        aq_score -= 40
    co_val = _parse_float(raw.get("carbonMonoxide"))
    if co_val is not None and co_val > 0:
        aq_score -= min(20, int(co_val / 2))
    humidity = _parse_float(raw.get("relativeHumidity"))
    if humidity is not None:
        if humidity > 70 or humidity < 30:
            aq_score -= 25
        elif humidity > 60 or humidity < 35:
            aq_score -= 10
    if _safe(raw.get("moldOrMildew"), "").lower() in ("yes", "true"):
        aq_score -= 20
    scores["air_quality"] = max(0, aq_score)

    scores["overall"] = round(
        scores["airflow"] * 0.20
        + scores["ductwork"] * 0.20
        + scores["refrigerant"] * 0.20
        + scores["electrical"] * 0.25
        + scores["air_quality"] * 0.15
    )
    return scores


# ---------------------------------------------------------------------------
# Plain-language finding cards
# ---------------------------------------------------------------------------

_STATUS_GOOD       = ("Good",          colors.HexColor("#EAF7EF"), colors.HexColor("#1F6B35"))
_STATUS_MONITOR    = ("Monitor",       colors.HexColor("#FFF8EE"), colors.HexColor("#7A4B00"))
_STATUS_REPAIR     = ("Repair needed", colors.HexColor("#FEF2F2"), colors.HexColor("#991B1B"))
_STATUS_INVESTIGATE = ("Investigate",  colors.HexColor("#FFF3CD"), colors.HexColor("#7D5A00"))


def _build_finding_cards(values, raw):
    cards = []

    def cond(key):
        return _safe(values.get(key), "No Data")

    # Temperature split
    temp_delta = _parse_float(raw.get("temperatureDelta"))
    temp_cond = cond("temperatureDeltaOverallCondition")
    if temp_delta is not None:
        if temp_cond == "Within Spec":
            cards.append({
                "title": "Your system is cooling effectively",
                "status": _STATUS_GOOD,
                "body": (
                    f"Your system is dropping the air temperature by {temp_delta:.0f}°F as it passes through "
                    f"- right in the ideal range of 16 - 22 °F. This means your equipment is transferring heat "
                    f"efficiently and your home should reach your thermostat setpoint without issue."
                ),
                "impact": "No comfort or cost impact. Your cooling performance is exactly where it should be.",
            })
        elif temp_cond == "Low":
            cards.append({
                "title": "Temperature split is lower than expected",
                "status": _STATUS_MONITOR,
                "body": (
                    f"Your system is only dropping the air temperature by {temp_delta:.0f}°F. The ideal range "
                    f"is 16 - 22 °F. A low split often points to low refrigerant, restricted airflow, malfunctioning "
                    f"metering device, blower fan issues, compressor, or load conditions - all of which reduce cooling "
                    f"efficiency."
                ),
                "impact": "Your system may struggle to reach setpoint on hot days and energy costs may rise.",
            })
        elif temp_cond == "High":
            cards.append({
                "title": "Temperature split is higher than expected",
                "status": _STATUS_MONITOR,
                "body": (
                    f"Your system is dropping the air temperature by {temp_delta:.0f}°F - above the ideal "
                    f"16 - 22 °F range. A very high split often signals restricted airflow that stresses the equipment."
                ),
                "impact": "Restricted airflow can lead to frozen evaporator coils and reduced equipment life.",
            })

    # Refrigerant charge
    superheat = _parse_float(raw.get("superheat"))
    target_sh = _parse_float(raw.get("targetSuperheat"))
    subcooling = _parse_float(raw.get("subcooling"))
    target_sc = _parse_float(raw.get("targetSubcooling"))
    ref_cond = cond("superheatSubcoolingOverallCondition")
    leaks = _safe(raw.get("leaks"), "No").lower()
    has_leaks = leaks not in ("no", "none", "")

    if has_leaks:
        cards.append({
            "title": "Refrigerant leak detected",
            "status": _STATUS_REPAIR,
            "body": (
                "A refrigerant leak was found during today's inspection. Refrigerant is the fluid that moves "
                "heat out of your home - when it leaks the system loses cooling capacity and runs longer to compensate."
            ),
            "impact": "An active leak means rising energy bills, reduced cooling, and possible compressor damage. This should be repaired promptly.",
        })
    elif ref_cond == "Within Spec":
        sh_detail = (
            f"Superheat measured {superheat:.0f}°F against a target of {target_sh:.0f}°F, and subcooling "
            f"measured {subcooling:.0f}°F against a target of {target_sc:.0f}°F. "
            if all(v is not None for v in [superheat, target_sh, subcooling, target_sc]) else ""
        )
        cards.append({
            "title": "Refrigerant charge is perfect",
            "status": _STATUS_GOOD,
            "body": (
                f"The refrigerant in your system is at exactly the right level. {sh_detail}"
                f"These are textbook readings with no leaks detected."
            ),
            "impact": "No impact. A properly charged system runs at peak efficiency, keeping your energy bills as low as possible.",
        })
    elif ref_cond in ("Low", "High"):
        sh_detail = (
            f"Superheat: {superheat:.0f}°F (target {target_sh:.0f}°F). Subcooling: {subcooling:.0f}°F (target {target_sc:.0f}°F). "
            if all(v is not None for v in [superheat, target_sh, subcooling, target_sc]) else ""
        )
        cards.append({
            "title": "Refrigerant charge is off",
            "status": _STATUS_MONITOR,
            "body": (
                f"Superheat and subcooling readings suggest the refrigerant charge is not at the ideal level. "
                f"{sh_detail}This can happen slowly from a small leak or equipment changes over time."
            ),
            "impact": "An improperly charged system runs less efficiently and puts added strain on the compressor.",
        })

    # Contactor
    contactor = _safe(raw.get("contactorCondition"), "").lower()
    if "bad" in contactor or "replace" in contactor or "fail" in contactor:
        cards.append({
            "title": "Contactor needs replacement",
            "status": _STATUS_REPAIR,
            "body": (
                "The contactor is the electrical switch that starts your outdoor compressor. Yours has reached "
                "the end of its serviceable life and should be replaced before the next run season."
            ),
            "impact": "A failed contactor will prevent the outdoor unit from running. Replacement is straightforward and typically costs $149 - $229.",
        })
    elif "questionable" in contactor or "warn" in contactor or "wear" in contactor or "monitor" in contactor:
        cards.append({
            "title": "Contactor is showing wear - watch closely",
            "status": _STATUS_MONITOR,
            "body": (
                "The contactor is an electrical switch that turns your compressor on and off every time the "
                "system runs - cycling hundreds of times per day in summer. Yours is showing signs of wear "
                "and may need replacement soon."
            ),
            "impact": (
                "If the contactor fails, your outdoor unit stops working entirely. Replacing it proactively "
                "costs $149 - $229. Waiting until it fails typically means an emergency call in peak summer heat at $350+."
            ),
        })

    # Ductwork
    supported = _safe(raw.get("wellSupported"), "").lower()
    duct_damaged = _safe(raw.get("damaged"), "").lower()
    duct_notes = _safe(raw.get("notesOnDuctwork"), "")
    if supported in ("no", "false") or duct_damaged in ("yes", "true"):
        note_suffix = f" Technician note: {duct_notes}" if duct_notes and duct_notes != "-" else ""
        cards.append({
            "title": "Duct support is damaged - needs repair",
            "status": _STATUS_REPAIR,
            "body": (
                "Some duct supports are broken, causing flexible duct to sag and kink. Think of it like a garden "
                "hose with a kink - air struggles to get through, even though your system is working hard to push it."
                + note_suffix
            ),
            "impact": (
                "Kinked ducts reduce airflow to specific rooms, make them harder to cool, and force your system "
                "to work harder - raising your energy bill. Typically a $150-$300 repair."
            ),
        })

    # Humidity
    humidity = _parse_float(raw.get("relativeHumidity"))
    if humidity is not None:
        if humidity > 70:
            cards.append({
                "title": "Indoor humidity is elevated",
                "status": _STATUS_MONITOR,
                "body": (
                    f"Your indoor relative humidity measured {humidity:.0f}%. The comfort zone is 40-60%. "
                    f"At {humidity:.0f}%, air feels muggy and warmer than the actual temperature - which often "
                    f"leads homeowners to lower the thermostat unnecessarily, increasing energy costs."
                ),
                "impact": "High humidity makes your home feel warmer than it is and costs more to cool. Over time it can encourage mold growth. A whole-home dehumidifier typically resolves this.",
            })
        elif humidity < 30:
            cards.append({
                "title": "Indoor humidity is too low",
                "status": _STATUS_MONITOR,
                "body": (
                    f"Your indoor relative humidity measured {humidity:.0f}%. The comfort zone is 40-60%. "
                    f"Very dry air can cause dry skin, static electricity, and damage to wood furnishings."
                ),
                "impact": "Low humidity can indicate over-ventilation. Consider a whole-home humidifier.",
            })

    # Carbon monoxide
    co_val = _parse_float(raw.get("carbonMonoxide"))
    if co_val is not None and co_val > 0:
        if co_val >= 35:
            cards.append({
                "title": "Dangerous carbon monoxide levels detected",
                "status": _STATUS_REPAIR,
                "body": (
                    f"Carbon monoxide measured {co_val:.0f} PPM - above the OSHA action level of 35 PPM. "
                    f"CO is an odorless, colorless gas produced by incomplete combustion. This level requires immediate attention."
                ),
                "impact": "Elevated CO is a serious health hazard. Have all combustion appliances inspected immediately and ensure CO detectors are functioning.",
            })
        else:
            cards.append({
                "title": "Low-level carbon monoxide detected",
                "status": _STATUS_INVESTIGATE,
                "body": (
                    f"Carbon monoxide measured {co_val:.0f} PPM in your living space. While below the alarm "
                    f"threshold of most CO detectors (70 PPM), the ideal level is zero. Any CO in living space "
                    f"warrants investigation into combustion appliances, attached garage, or neighboring units."
                ),
                "impact": "Chronic low-level CO exposure can cause headaches and fatigue. Have gas appliances and venting inspected. Ensure CO detectors are less than 5 years old and functioning.",
            })

    # Compressor amperage
    comp_cond = cond("compressorAmperageOverallCondition")
    comp_amps = _parse_float(raw.get("compressorAmperage"))
    target_amps = _parse_float(raw.get("targetCompressorAmperage"))
    if comp_cond == "High" and comp_amps and target_amps:
        cards.append({
            "title": "Compressor is drawing too much power",
            "status": _STATUS_MONITOR,
            "body": (
                f"Your compressor is drawing {comp_amps:.0f}A against a rated target of {target_amps:.0f}A. "
                f"High amperage means the motor is working harder than it should, often due to mechanical wear, "
                f"refrigerant issues, or restricted airflow."
            ),
            "impact": "Sustained high amperage shortens compressor life and increases energy costs. Compressor replacement can cost $900 - $2,500+.",
        })

    return cards


# ---------------------------------------------------------------------------
# Recommendations table logic
# ---------------------------------------------------------------------------

def _build_recommendations(values, raw):
    recs = []

    def cond(key):
        return _safe(values.get(key), "No Data")

    co_val = _parse_float(raw.get("carbonMonoxide"))
    if co_val and co_val >= 35:
        recs.append({"priority": "Urgent", "order": 0, "item": "CO safety inspection", "action": f"{co_val:.0f} PPM CO detected. Immediate inspection of all combustion appliances required."})
    elif co_val and co_val > 0:
        recs.append({"priority": "Investigate", "order": 1, "item": "CO source investigation", "action": f"{co_val:.0f} PPM CO detected. Check all combustion appliances and venting."})

    leaks = _safe(raw.get("leaks"), "No").lower()
    if leaks not in ("no", "none", ""):
        recs.append({"priority": "Repair needed", "order": 2, "item": "Refrigerant leak repair", "action": "Locate and repair refrigerant leak, then recharge system to proper level."})

    contactor = _safe(raw.get("contactorCondition"), "").lower()
    if "bad" in contactor or "replace" in contactor or "fail" in contactor:
        recs.append({"priority": "Repair needed", "order": 2, "item": "Contactor replacement", "action": "Contactor has failed - replace before next run season. Est. $149 - $229."})
    elif "questionable" in contactor or "warn" in contactor or "wear" in contactor or "monitor" in contactor:
        recs.append({"priority": "Soon", "order": 3, "item": "Contactor replacement", "action": "Proactive replacement before cooling season. Est. $149 - $229."})

    supported = _safe(raw.get("wellSupported"), "").lower()
    duct_damaged = _safe(raw.get("damaged"), "").lower()
    if supported in ("no", "false") or duct_damaged in ("yes", "true"):
        recs.append({"priority": "Soon", "order": 3, "item": "Duct support repair", "action": "Repair broken supports and reposition flex duct. Est. $150 - $300."})

    if cond("compressorAmperageOverallCondition") == "High":
        recs.append({"priority": "Soon", "order": 3, "item": "Compressor inspection", "action": "High amperage detected - inspect for wear and causes of overload."})

    humidity = _parse_float(raw.get("relativeHumidity"))
    if humidity and humidity > 60:
        recs.append({"priority": "Monitor", "order": 4, "item": "Humidity management", "action": f"{humidity:.0f}% RH elevated. Evaluate runtime or consider dehumidifier."})

    if cond("staticPressureOverallCondition") in ("High", "Low"):
        recs.append({"priority": "Monitor", "order": 4, "item": "Static pressure review", "action": "Static pressure outside design range - duct sizing or filter restriction worth investigating."})

    ref_cond = cond("superheatSubcoolingOverallCondition")
    if ref_cond in ("High", "Low") and leaks in ("no", "none", ""):
        recs.append({"priority": "Monitor", "order": 4, "item": "Refrigerant charge check", "action": "Superheat/subcooling slightly off. Recheck at next visit to confirm trend."})

    if not recs:
        recs.append({"priority": "None needed", "order": 9, "item": "Refrigerant circuit", "action": "System perfectly charged, no leaks. No action required."})
        recs.append({"priority": "None needed", "order": 9, "item": "Routine maintenance", "action": "All readings within spec. Continue scheduled maintenance and filter changes."})

    recs.sort(key=lambda r: r["order"])
    return recs


# ---------------------------------------------------------------------------
# PDF style helpers
# ---------------------------------------------------------------------------

def _make_styles():
    base = getSampleStyleSheet()
    return {
        "business_name":         ParagraphStyle("BizName",      parent=base["Normal"], fontSize=16, fontName="Helvetica-Bold",    textColor=colors.white,                    leading=20),
        "report_label":          ParagraphStyle("ReportLabel",  parent=base["Normal"], fontSize=10, fontName="Helvetica",         textColor=colors.HexColor("#C8D8EC"),      leading=13),
        "meta_label":            ParagraphStyle("MetaLabel",    parent=base["Normal"], fontSize=8.5, fontName="Helvetica-Bold",   textColor=colors.HexColor("#8FA8C8"),      leading=11),
        "meta_value":            ParagraphStyle("MetaValue",    parent=base["Normal"], fontSize=8.5, fontName="Helvetica",        textColor=colors.white,                    leading=11),
        "section_title":         ParagraphStyle("SecTitle",     parent=base["Normal"], fontSize=11,  fontName="Helvetica-Bold",   textColor=colors.HexColor("#122136"),      leading=14),
        "section_subtitle":      ParagraphStyle("SecSub",       parent=base["Normal"], fontSize=8.5, fontName="Helvetica",        textColor=colors.HexColor("#6B7E99"),      leading=11),
        "score_num":             ParagraphStyle("ScoreNum",     parent=base["Normal"], fontSize=28,  fontName="Helvetica-Bold",   textColor=colors.HexColor("#122136"),      leading=32),
        "score_label":           ParagraphStyle("ScoreLabel",   parent=base["Normal"], fontSize=8,   fontName="Helvetica",        textColor=colors.HexColor("#6B7E99"),      leading=10),
        "cat_name":              ParagraphStyle("CatName",      parent=base["Normal"], fontSize=9,   fontName="Helvetica",        textColor=colors.HexColor("#2A3A54"),      leading=11),
        "cat_score":             ParagraphStyle("CatScore",     parent=base["Normal"], fontSize=9,   fontName="Helvetica-Bold",   textColor=colors.HexColor("#122136"),      leading=11),
        "finding_title":         ParagraphStyle("FindTitle",    parent=base["Normal"], fontSize=10.5, fontName="Helvetica-Bold",  textColor=colors.HexColor("#122136"),      leading=13),
        "finding_body":          ParagraphStyle("FindBody",     parent=base["Normal"], fontSize=9,   fontName="Helvetica",        textColor=colors.HexColor("#2A3A54"),      leading=12),
        "finding_impact":        ParagraphStyle("FindImpact",   parent=base["Normal"], fontSize=8.5, fontName="Helvetica-Oblique", textColor=colors.HexColor("#5A6B82"),     leading=11),
        "detail_section_header": ParagraphStyle("DSH",          parent=base["Normal"], fontSize=10,  fontName="Helvetica-Bold",   textColor=colors.white,                    leading=13),
        "detail_section_sub":    ParagraphStyle("DSS",          parent=base["Normal"], fontSize=8,   fontName="Helvetica",        textColor=colors.HexColor("#B8CDE6"),      leading=10),
        "detail_label":          ParagraphStyle("DL",           parent=base["Normal"], fontSize=9,   fontName="Helvetica",        textColor=colors.HexColor("#2A3A54"),      leading=11),
        "detail_value":          ParagraphStyle("DV",           parent=base["Normal"], fontSize=9,   fontName="Helvetica-Bold",   textColor=colors.HexColor("#122136"),      leading=11),
        "rec_pri_text":          ParagraphStyle("RecPri",       parent=base["Normal"], fontSize=8.5, fontName="Helvetica-Bold",   textColor=colors.HexColor("#122136"),      leading=11),
        "rec_item":              ParagraphStyle("RecItem",      parent=base["Normal"], fontSize=9,   fontName="Helvetica-Bold",   textColor=colors.HexColor("#122136"),      leading=11),
        "rec_action":            ParagraphStyle("RecAction",    parent=base["Normal"], fontSize=9,   fontName="Helvetica",        textColor=colors.HexColor("#2A3A54"),      leading=12),
        "footer":                ParagraphStyle("Footer",       parent=base["Normal"], fontSize=8,   fontName="Helvetica",        textColor=colors.HexColor("#8FA8C8"),      alignment=1, leading=10),
    }


def _score_bar(score, width=1.3 * inch, height=6):
    filled = max(0.0, min(1.0, score / 100.0))
    bar_color = (
        colors.HexColor("#1F6B35") if score >= 80
        else colors.HexColor("#7A4B00") if score >= 55
        else colors.HexColor("#991B1B")
    )
    filled_w = filled * width
    empty_w = width - filled_w
    if empty_w <= 0:
        cells, col_widths = [[""]], [width]
    else:
        cells, col_widths = [["", ""]], [filled_w, empty_w]

    bar = Table(cells, colWidths=col_widths, rowHeights=[height])
    style_cmds = [
        ("BACKGROUND", (0, 0), (0, -1), bar_color),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("BOX", (0, 0), (-1, -1), 0.3, colors.HexColor("#D1D5DB")),
    ]
    if len(col_widths) == 2:
        style_cmds.append(("BACKGROUND", (1, 0), (1, -1), colors.HexColor("#EBEBEB")))
    bar.setStyle(TableStyle(style_cmds))
    return bar


def _status_badge(status_tuple, width=1.0 * inch):
    label, bg, fg = status_tuple
    badge = Table([[label]], colWidths=[width])
    badge.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), bg),
        ("TEXTCOLOR", (0, 0), (-1, -1), fg),
        ("FONT", (0, 0), (-1, -1), "Helvetica-Bold", 8),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("BOX", (0, 0), (-1, -1), 0.5, fg),
    ]))
    return badge


def _priority_color(priority):
    p = priority.lower()
    if "urgent" in p or "repair" in p:
        return colors.HexColor("#991B1B"), colors.HexColor("#FEF2F2")
    if "soon" in p:
        return colors.HexColor("#7A4B00"), colors.HexColor("#FFF8EE")
    if "investigate" in p:
        return colors.HexColor("#7D5A00"), colors.HexColor("#FFF3CD")
    if "monitor" in p:
        return colors.HexColor("#125086"), colors.HexColor("#E7F2FD")
    return colors.HexColor("#1F6B35"), colors.HexColor("#EAF7EF")


# ---------------------------------------------------------------------------
# Main PDF assembly
# ---------------------------------------------------------------------------

def generate_hvac_system_health_report(
    hvac_system_id,
    customer,
    hvac_system,
    diagnostics_card,
    report_number,
    raw_diagnostics=None,
    business=None,
):
    """Generate a customer-facing HVAC diagnostic PDF report."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"hvac_report_{hvac_system_id}_{timestamp}.pdf"
    reports_dir = os.path.join(os.path.dirname(__file__), "invoices")
    os.makedirs(reports_dir, exist_ok=True)
    filepath = os.path.join(reports_dir, filename)

    doc = SimpleDocTemplate(
        filepath,
        pagesize=letter,
        topMargin=0.4 * inch,
        bottomMargin=0.5 * inch,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
    )

    S = _make_styles()
    story = []

    values = diagnostics_card.get("values", {}) if isinstance(diagnostics_card, dict) else {}
    raw = raw_diagnostics if isinstance(raw_diagnostics, dict) else {}
    business = business or {}

    business_name = (
        str(business.get("company_name") or business.get("business_name") or "").strip()
        or "Your HVAC Service Company"
    )
    customer_name = " ".join(
        p for p in [
            str(customer.get("first_name", "")).strip(),
            str(customer.get("last_name", "")).strip(),
        ] if p
    ).strip() or "Customer"

    system_type = _safe(hvac_system.get("system_type"), "HVAC System")
    date_performed = _safe(diagnostics_card.get("date_performed"), "-")

    # ------------------------------------------------------------------ #
    # HEADER BAND                                                          #
    # ------------------------------------------------------------------ #
    header_left = Table(
        [
            [Paragraph(business_name, S["business_name"])],
            [Paragraph("DIAGNOSTIC REPORT", S["report_label"])],
        ],
        colWidths=[3.5 * inch],
    )
    header_left.setStyle(TableStyle([
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))

    meta_rows = [
        [Paragraph("Date performed", S["meta_label"]), Paragraph(date_performed, S["meta_value"])],
        [Paragraph("System type", S["meta_label"]), Paragraph(system_type, S["meta_value"])],
        [Paragraph("Report ID", S["meta_label"]), Paragraph(_safe(report_number), S["meta_value"])],
        [Paragraph("Customer", S["meta_label"]), Paragraph(customer_name, S["meta_value"])],
    ]
    header_right = Table(meta_rows, colWidths=[1.1 * inch, 2.15 * inch])
    header_right.setStyle(TableStyle([
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]))

    header_band = Table(
        [[header_left, header_right]],
        colWidths=[3.5 * inch, 3.45 * inch],
    )
    header_band.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#122136")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
    ]))

    story.append(header_band)
    story.append(Spacer(1, 0.18 * inch))

    # ------------------------------------------------------------------ #
    # OVERALL SYSTEM HEALTH SCORES                                         #
    # ------------------------------------------------------------------ #
    scores = _compute_health_scores(values, raw)

    overall_block = Table(
        [
            [Paragraph(f"{scores['overall']}/100", S["score_num"])],
            [Paragraph("Overall score", S["score_label"])],
        ],
        colWidths=[1.5 * inch],
    )
    overall_block.setStyle(TableStyle([
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))

    category_rows = []
    for cat_label, cat_key in [
        ("Airflow", "airflow"),
        ("Refrigerant circuit", "refrigerant"),
        ("Electrical", "electrical"),
        ("Ductwork", "ductwork"),
        ("Air quality", "air_quality"),
    ]:
        cat_score = scores[cat_key]
        category_rows.append([
            Paragraph(cat_label, S["cat_name"]),
            _score_bar(cat_score),
            Paragraph(f"{cat_score}/100", S["cat_score"]),
        ])

    category_table = Table(category_rows, colWidths=[1.55 * inch, 1.45 * inch, 0.7 * inch])
    category_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))

    health_inner = Table(
        [[overall_block, category_table]],
        colWidths=[1.7 * inch, 5.05 * inch],
    )
    health_inner.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("LINEBEFORE", (1, 0), (1, -1), 0.6, colors.HexColor("#D7E1EE")),
    ]))

    health_title = Table(
        [[Paragraph("Overall system health", S["section_title"])]],
        colWidths=[6.75 * inch],
    )
    health_title.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F0F4FA")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#D7E1EE")),
        ("LINEBELOW", (0, 0), (-1, -1), 0.5, colors.HexColor("#D7E1EE")),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))

    health_body = Table([[health_inner]], colWidths=[6.75 * inch])
    health_body.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.white),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#D7E1EE")),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
    ]))

    story.append(KeepTogether([health_title, health_body]))
    story.append(Spacer(1, 0.2 * inch))

    # ------------------------------------------------------------------ #
    # FINDING CARDS                                                        #
    # ------------------------------------------------------------------ #
    finding_cards = _build_finding_cards(values, raw)

    if finding_cards:
        found_header = Table(
            [
                [Paragraph("What we found: ", S["section_title"])],
            ],
            colWidths=[6.75 * inch],
        )
        found_header.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F0F4FA")),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#D7E1EE")),
            ("LEFTPADDING", (0, 0), (-1, -1), 10),
            ("RIGHTPADDING", (0, 0), (-1, -1), 10),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(found_header)

        for card in finding_cards:
            status_label, status_bg, status_fg = card["status"]
            badge_w = max(0.85 * inch, len(status_label) * 6.5)

            title_row = Table(
                [[Paragraph(card["title"], S["finding_title"]), _status_badge(card["status"], width=badge_w)]],
                colWidths=[6.55 * inch - badge_w - 0.2 * inch, badge_w + 0.15 * inch],
            )
            title_row.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ]))

            card_rows = [
                [title_row],
                [Paragraph(card["body"], S["finding_body"])],
            ]
            if card.get("impact"):
                card_rows.append([Paragraph(card["impact"], S["finding_impact"])])

            card_table = Table(card_rows, colWidths=[6.55 * inch])
            card_style = [
                ("BACKGROUND", (0, 0), (-1, -1), colors.white),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#D7E1EE")),
                ("LINEABOVE", (0, 0), (-1, 0), 3, status_bg),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
            ]
            if len(card_rows) > 2:
                card_style.append(("LINEBELOW", (0, 1), (-1, 1), 0.3, colors.HexColor("#E8EEF7")))
            card_table.setStyle(TableStyle(card_style))

            story.append(KeepTogether(card_table))

        story.append(Spacer(1, 0.2 * inch))

    # ------------------------------------------------------------------ #
    # DETAIL SECTIONS                                                      #
    # ------------------------------------------------------------------ #
    for section_label, section_sub, fields in _REPORT_SECTIONS:
        rows = []
        for field_name, field_label, unit in fields:
            raw_val = raw.get(field_name, "")
            display = _format_value(field_name, raw_val, unit)
            if display and display != "-":
                rows.append((field_label, display))
        if not rows:
            continue

        sec_header = Table(
            [[Paragraph(section_label, S["detail_section_header"]), Paragraph(section_sub, S["detail_section_sub"])]],
            colWidths=[3.4 * inch, 3.35 * inch],
        )
        sec_header.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#1C314D")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 10),
            ("RIGHTPADDING", (0, 0), (-1, -1), 10),
            ("TOPPADDING", (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ]))

        detail_rows = []
        for lbl, val in rows:
            detail_rows.append([Paragraph(lbl, S["detail_label"]), Paragraph(val, S["detail_value"])])

        detail_table = Table(detail_rows, colWidths=[2.9 * inch, 3.85 * inch])
        det_style = [
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#D7E1EE")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 10),
            ("RIGHTPADDING", (0, 0), (-1, -1), 10),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, colors.HexColor("#F7FAFD")]),
        ]
        for i in range(len(detail_rows) - 1):
            det_style.append(("LINEBELOW", (0, i), (-1, i), 0.3, colors.HexColor("#DFE6F0")))
        detail_table.setStyle(TableStyle(det_style))

        story.append(KeepTogether([sec_header, detail_table]))

        section_photos = _section_photo_entries_for_report(raw, section_label)
        photo_cards = []
        for photo in section_photos:
            photo_path = _resolve_photo_absolute_path(photo)
            if not photo_path:
                continue

            max_width = 3.15 * inch
            max_height = 2.0 * inch
            image_flowable = _build_pdf_photo_flowable(photo_path, max_width=max_width, max_height=max_height)
            if not image_flowable:
                continue

            caption_text = str(photo.get("caption") or "").strip()
            card_rows = [[image_flowable]]
            if caption_text:
                card_rows.append([Paragraph(caption_text, S["detail_value"])])

            photo_card = Table(card_rows, colWidths=[3.15 * inch])
            photo_card.setStyle(TableStyle([
                ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#D7E1EE")),
                ("BACKGROUND", (0, 0), (-1, -1), colors.white),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]))
            photo_cards.append(photo_card)

        if photo_cards:
            photo_rows = []
            for index in range(0, len(photo_cards), 2):
                left_card = photo_cards[index]
                right_card = photo_cards[index + 1] if (index + 1) < len(photo_cards) else Spacer(1, 0.01 * inch)
                photo_rows.append([left_card, right_card])

            photo_header = Table([[Paragraph("Section photos", S["section_subtitle"])]], colWidths=[6.75 * inch])
            photo_header.setStyle(TableStyle([
                ("LEFTPADDING", (0, 0), (-1, -1), 2),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]))

            photo_grid = Table(photo_rows, colWidths=[3.3 * inch, 3.3 * inch])
            photo_grid.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]))

            story.append(photo_header)
            story.append(photo_grid)

        story.append(Spacer(1, 0.12 * inch))

    # ------------------------------------------------------------------ #
    # RECOMMENDATIONS TABLE                                                #
    # ------------------------------------------------------------------ #
    recs = _build_recommendations(values, raw)

    rec_header_block = Table(
        [
            [Paragraph("Recommendations", S["section_title"])],
            [Paragraph("Prioritized action items from today's inspection.", S["section_subtitle"])],
        ],
        colWidths=[6.75 * inch],
    )
    rec_header_block.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F0F4FA")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#D7E1EE")),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))

    rec_data = [[
        Paragraph("Priority", S["detail_label"]),
        Paragraph("Item", S["detail_label"]),
        Paragraph("Action", S["detail_label"]),
    ]]
    for rec in recs:
        text_color, bg_color = _priority_color(rec["priority"])
        chip = Table([[rec["priority"]]], colWidths=[1.05 * inch])
        chip.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), bg_color),
            ("TEXTCOLOR", (0, 0), (-1, -1), text_color),
            ("FONT", (0, 0), (-1, -1), "Helvetica-Bold", 8),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("BOX", (0, 0), (-1, -1), 0.5, text_color),
        ]))
        rec_data.append([chip, Paragraph(rec["item"], S["rec_item"]), Paragraph(rec["action"], S["rec_action"])])

    rec_table = Table(rec_data, colWidths=[1.2 * inch, 1.9 * inch, 3.65 * inch])
    rec_style = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EDF2F8")),
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8.5),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#2A3A54")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#D7E1EE")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F7FAFD")]),
    ]
    for i in range(len(rec_data) - 1):
        rec_style.append(("LINEBELOW", (0, i), (-1, i), 0.3, colors.HexColor("#DFE6F0")))
    rec_table.setStyle(TableStyle(rec_style))

    story.append(KeepTogether([rec_header_block, rec_table]))
    story.append(Spacer(1, 0.22 * inch))

    # ------------------------------------------------------------------ #
    # FOOTER                                                               #
    # ------------------------------------------------------------------ #
    now = datetime.now()
    try:
        date_str = now.strftime("%-d %B %Y")  # Linux/macOS non-padded
    except ValueError:
        date_str = now.strftime("%d %B %Y").lstrip("0")

    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#D7E1EE")))
    story.append(Spacer(1, 0.06 * inch))
    story.append(Paragraph(
        f"{business_name}     -     Powered by Klivent     -     Report generated {date_str}",
        S["footer"],
    ))

    doc.build(story)
    return filepath
