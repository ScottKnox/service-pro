from datetime import datetime
import json
import os
import re

from bson import ObjectId
from flask import Blueprint, current_app, redirect, render_template, request, session, url_for

from mongo import build_reference_filter, ensure_connection_or_500, object_id_or_404, reference_value, serialize_doc
from hvac_report_generator import generate_hvac_system_health_report
from utils.catalog import build_job_parts_from_form, build_part_catalog
from utils.csv_export import build_csv_export_response

bp = Blueprint("customers", __name__)


def _resolve_current_business_id(db):
    employee_id = session.get("employee_id")
    if not employee_id or not ObjectId.is_valid(employee_id):
        return None

    employee = db.employees.find_one({"_id": ObjectId(employee_id)}, {"business": 1})
    business_ref = (employee or {}).get("business")
    if isinstance(business_ref, ObjectId):
        return business_ref
    if isinstance(business_ref, str) and ObjectId.is_valid(business_ref):
        return ObjectId(business_ref)
    return None

EMAIL_VALIDATION_MESSAGE = "Enter a valid email address."
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

CUSTOMER_TYPE_OPTIONS = (
    "Residential",
    "Commercial",
    "Industrial",
    "Institutional",
    "Specialty",
)

PROPERTY_TYPE_OPTIONS_BY_CUSTOMER_TYPE = {
    "Residential": [
        "single_family_home",
        "condo_townhouse",
        "apartment",
        "mobile_home",
        "new_construction",
        "other",
    ],
    "Commercial": [
        "office_building",
        "retail_store",
        "restaurant",
        "hotel",
        "medical_office",
        "gym",
        "car_dealership",
        "bank",
        "other",
    ],
    "Industrial": [
        "warehouse",
        "manufacturing_facility",
        "refrigeration_facility",
        "data_center",
        "auto_shop",
        "other",
    ],
    "Institutional": [
        "school",
        "hospital",
        "government_building",
        "church",
        "community_center",
        "other",
    ],
    "Specialty": [
        "multi_tenant_commercial_building",
        "hoa_managed_community",
        "property_management_portfolio",
        "new_construction_builder_contract",
        "other",
    ],
}

PROPERTY_TYPE_LABELS = {
    "single_family_home": "Single-family home",
    "condo_townhouse": "Condo & townhouse",
    "apartment": "Apartment",
    "mobile_home": "Mobile home",
    "new_construction": "New construction",
    "office_building": "Office building",
    "retail_store": "Retail store",
    "restaurant": "Restaurant",
    "hotel": "Hotel",
    "medical_office": "Medical office",
    "gym": "Gym",
    "car_dealership": "Car dealership",
    "bank": "Bank",
    "warehouse": "Warehouse",
    "manufacturing_facility": "Manufacturing facility",
    "refrigeration_facility": "Refrigeration facility",
    "data_center": "Data center",
    "auto_shop": "Auto shop",
    "school": "School",
    "hospital": "Hospital",
    "government_building": "Government building",
    "church": "Church",
    "community_center": "Community center",
    "multi_tenant_commercial_building": "Multi-tenant commercial building (landlord contract)",
    "hoa_managed_community": "HOA-managed community",
    "property_management_portfolio": "Property management portfolio (one contract covering many units)",
    "new_construction_builder_contract": "New construction (builder contract - ongoing relationship across multiple builds)",
    "other": "Other",
}


def _normalize_customer_type(raw_type):
    customer_type = str(raw_type or "").strip().title()
    if customer_type in CUSTOMER_TYPE_OPTIONS:
        return customer_type
    return "Residential"


def _get_property_type_options(customer_type):
    normalized_type = _normalize_customer_type(customer_type)
    return PROPERTY_TYPE_OPTIONS_BY_CUSTOMER_TYPE.get(normalized_type, PROPERTY_TYPE_OPTIONS_BY_CUSTOMER_TYPE["Residential"])


def _property_type_label(property_type):
    normalized_property_type = str(property_type or "").strip()
    if not normalized_property_type:
        return "-"
    return PROPERTY_TYPE_LABELS.get(normalized_property_type, normalized_property_type.replace("_", " ").title())


def _get_customer_properties(customer):
    raw_properties = (customer or {}).get("properties", [])
    if not isinstance(raw_properties, list):
        return []

    normalized_properties = []
    for prop in raw_properties:
        if not isinstance(prop, dict):
            continue
        property_id = str(prop.get("property_id") or "").strip() or str(ObjectId())
        property_type = str(prop.get("property_type") or "").strip()
        is_default = bool(prop.get("is_default"))
        normalized_properties.append(
            {
                "property_id": property_id,
                "property_name": str(prop.get("property_name") or "").strip(),
                "property_type": property_type,
                "property_type_label": _property_type_label(property_type),
                "property_type_other": str(prop.get("property_type_other") or "").strip(),
                "address_line_1": str(prop.get("address_line_1") or "").strip(),
                "address_line_2": str(prop.get("address_line_2") or "").strip(),
                "city": str(prop.get("city") or "").strip(),
                "state": str(prop.get("state") or "").strip().upper(),
                "zip_code": str(prop.get("zip_code") or "").strip(),
                "is_default": is_default,
                "is_seed_primary_address": bool(prop.get("is_seed_primary_address")),
            }
        )

    # Enforce a single default property in-memory for rendering and downstream logic.
    default_count = sum(1 for prop in normalized_properties if prop.get("is_default"))
    if default_count > 1:
        first_default_found = False
        for prop in normalized_properties:
            if prop.get("is_default") and not first_default_found:
                first_default_found = True
                continue
            prop["is_default"] = False

    return normalized_properties


def _find_customer_property(customer, property_id):
    normalized_property_id = str(property_id or "").strip()
    if not normalized_property_id:
        return None

    for prop in _get_customer_properties(customer):
        if prop.get("property_id") == normalized_property_id:
            return prop
    return None


def _normalize_property_payload(form_data, customer_type):
    property_type = str(form_data.get("property_type") or "").strip()
    property_type_other = str(form_data.get("property_type_other") or "").strip()
    if property_type == "other" and property_type_other:
        property_type_value = property_type_other
    else:
        property_type_value = property_type

    return {
        "property_id": str(form_data.get("property_id") or "").strip() or str(ObjectId()),
        "property_name": str(form_data.get("property_name") or "").strip(),
        "property_type": property_type_value,
        "property_type_other": property_type_other,
        "address_line_1": str(form_data.get("address_line_1") or "").strip(),
        "address_line_2": str(form_data.get("address_line_2") or "").strip(),
        "city": str(form_data.get("city") or "").strip(),
        "state": str(form_data.get("state") or "").strip().upper(),
        "zip_code": str(form_data.get("zip_code") or "").strip(),
        "is_default": bool(form_data.get("is_default")),
        "is_seed_primary_address": bool(form_data.get("is_seed_primary_address")),
    }


def _property_payload_is_valid(property_payload):
    required_values = (
        property_payload.get("property_name"),
        property_payload.get("property_type"),
        property_payload.get("address_line_1"),
        property_payload.get("city"),
        property_payload.get("state"),
        property_payload.get("zip_code"),
    )
    return all(str(value or "").strip() for value in required_values)

HVAC_COLLECTION_CONFIG = {
    "Split": (("airHandlers", "Air Handler"), ("condensers", "Condenser")),
    "Heat Pump": (("airHandlers", "Air Handler"), ("condensers", "Condenser")),
    "Package": (("packageUnits", "Unit"),),
    "Mini Split": (("miniSplits", "Unit"),),
}

HVAC_COMPONENT_LABELS = {
    "airHandlers": "Air Handler",
    "condensers": "Condenser",
    "packageUnits": "Unit",
    "miniSplits": "Unit",
}

HVAC_COMPONENT_FIELD_BY_COLLECTION = {
    "airHandlers": "air_handler",
    "condensers": "condenser",
    "packageUnits": "unit",
    "miniSplits": "unit",
}

SYSTEM_TYPE_OPTIONS = ("Heat Pump", "Split", "Package", "Mini Split")

TONNAGE_OPTIONS = (
    ".50 Ton (6000 BTU)",
    ".75 Ton (9000 BTU)",
    "1 Ton (12000 BTU)",
    "1.5 Tons (18000 BTU)",
    "2 Ton (24000 BTU)",
    "2.5 Tons (30000 BTU)",
    "3 Ton (36000 BTU)",
    "3.5 Tons (42000 BTU)",
    "4 Ton (48000 BTU)",
    "4.5 Ton (54000 BTU)",
    "5 Ton (60000 BTU)",
)

LOCATION_TYPE_OPTIONS = (
    "Primary Residence",
    "Shop Building",
    "Secondary Home",
    "External Building",
    "Apartment",
    "Townhouse",
)

DUCTWORK_SYSTEM_TYPES = {"Split", "Heat Pump", "Package"}

DUCTWORK_TYPE_OPTIONS = (
    "Sheet Metal and Flex",
    "Fiberglass Lined Sheet Metal and Flex",
    "Sheet Metal",
    "Flex",
    "Fiberboard",
)

INSULATED_OPTIONS = ("No", "Yes")

REFRIGERANT_TYPE_OPTIONS = (
    "R-22",
    "R-410A",
    "R-32",
    "R-454B",
    "R-134a",
    "R-407C",
    "Other",
)

ELECTRICAL_STATUS_OPTIONS = (
    "Within Spec",
    "Out of Spec",
    "Near End of Life",
    "Failed",
)

DIAGNOSTIC_YES_NO_OPTIONS = (
    "Yes",
    "No",
)

MANUFACTURER_OPTIONS = (
    "Trane",
    "Lennox",
    "Carrier",
    "Goodman",
    "Amana",
    "Rheem",
    "Daikin",
    "American Standard",
)

HVAC_DIAGNOSTIC_SECTIONS = (
    (
        "Airflow",
        (
            {"name": "supplyStaticPressure", "label": "Supply Static Pressure", "type": "text", "required": False},
            {"name": "returnStaticPressure", "label": "Return Static Pressure", "type": "text", "required": False},
            {
                "name": "totalExternalStaticPressure",
                "label": "Total External Static Pressure",
                "type": "text",
                "required": False,
                "readonly": True,
            },
            {
                "name": "designStaticPressure",
                "label": "Design Static Pressure",
                "type": "text",
                "required": False,
            },
            {"name": "actualCfm", "label": "Actual CFM", "type": "text", "required": False},
            {
                "name": "designCfm",
                "label": "Design CFM",
                "type": "text",
                "required": False,
                "readonly": True,
            },
            {"name": "temperatureDelta", "label": "Temperature Delta", "type": "text", "required": False},
            {
                "name": "staticPressureNotes",
                "label": "Static Pressure Notes",
                "type": "textarea",
                "required": False,
            },
        ),
    ),
    (
        "Refrigerant",
        (
            {
                "name": "refrigerantType",
                "label": "Refrigerant Type",
                "type": "select",
                "required": False,
                "options": REFRIGERANT_TYPE_OPTIONS,
            },
            {"name": "suctionPressure", "label": "Suction Pressure", "type": "text", "required": False},
            {
                "name": "targetSuctionPressure",
                "label": "Target Suction Pressure",
                "type": "text",
                "required": False,
            },
            {"name": "highSidePressure", "label": "High Side Pressure", "type": "text", "required": False},
            {
                "name": "targetHighSidePressure",
                "label": "Target High Side Pressure",
                "type": "text",
                "required": False,
            },
            {"name": "superheat", "label": "Superheat", "type": "text", "required": False},
            {"name": "targetSuperheat", "label": "Target Superheat", "type": "text", "required": False},
            {"name": "subcooling", "label": "Subcooling", "type": "text", "required": False},
            {"name": "targetSubcooling", "label": "Target Subcooling", "type": "text", "required": False},
            {
                "name": "notesOnRefrigerant",
                "label": "Notes on Refrigerant",
                "type": "textarea",
                "required": False,
            },
        ),
    ),
    (
        "Electrical",
        (
            {
                "name": "supplyVoltage",
                "label": "Supply Voltage",
                "type": "select",
                "required": False,
                "options": ELECTRICAL_STATUS_OPTIONS,
            },
            {
                "name": "compressorRla",
                "label": "Compressor RLA",
                "type": "select",
                "required": False,
                "options": ELECTRICAL_STATUS_OPTIONS,
            },
            {
                "name": "capacitors",
                "label": "Capacitors",
                "type": "select",
                "required": False,
                "options": ELECTRICAL_STATUS_OPTIONS,
            },
            {
                "name": "thermostat",
                "label": "Thermostat",
                "type": "select",
                "required": False,
                "options": ELECTRICAL_STATUS_OPTIONS,
            },
            {
                "name": "transformers",
                "label": "Transformers",
                "type": "select",
                "required": False,
                "options": ELECTRICAL_STATUS_OPTIONS,
            },
            {
                "name": "blowerMotorVoltage",
                "label": "Blower Motor Voltage",
                "type": "select",
                "required": False,
                "options": ELECTRICAL_STATUS_OPTIONS,
            },
            {
                "name": "condenserFanVoltage",
                "label": "Condenser Fan Voltage",
                "type": "select",
                "required": False,
                "options": ELECTRICAL_STATUS_OPTIONS,
            },
            {
                "name": "electricalNotes",
                "label": "Electrical Notes",
                "type": "textarea",
                "required": False,
            },
        ),
    ),
    (
        "Ductwork",
        (
            {
                "name": "properlySized",
                "label": "Properly Sized?",
                "type": "select",
                "required": False,
                "options": DIAGNOSTIC_YES_NO_OPTIONS,
            },
            {
                "name": "leaks",
                "label": "Leaks?",
                "type": "select",
                "required": False,
                "options": DIAGNOSTIC_YES_NO_OPTIONS,
            },
            {
                "name": "wellInsulated",
                "label": "Well-Insulated?",
                "type": "select",
                "required": False,
                "options": DIAGNOSTIC_YES_NO_OPTIONS,
            },
            {
                "name": "wellSupported",
                "label": "Well-Supported?",
                "type": "select",
                "required": False,
                "options": DIAGNOSTIC_YES_NO_OPTIONS,
            },
            {
                "name": "damaged",
                "label": "Damaged?",
                "type": "select",
                "required": False,
                "options": DIAGNOSTIC_YES_NO_OPTIONS,
            },
            {
                "name": "clean",
                "label": "Clean?",
                "type": "select",
                "required": False,
                "options": DIAGNOSTIC_YES_NO_OPTIONS,
            },
            {
                "name": "dampersFunctioningProperly",
                "label": "Dampers Functioning Properly?",
                "type": "select",
                "required": False,
                "options": DIAGNOSTIC_YES_NO_OPTIONS,
            },
            {
                "name": "notesOnDuctwork",
                "label": "Notes on Ductwork",
                "type": "textarea",
                "required": False,
            },
        ),
    ),
    (
        "Indoor Air Quality",
        (
            {
                "name": "relativeHumidity",
                "label": "Relative Humidity",
                "type": "text",
                "required": False,
            },
            {
                "name": "carbonDioxide",
                "label": "Carbon Dioxide (CO2)",
                "type": "text",
                "required": False,
            },
            {
                "name": "carbonMonoxide",
                "label": "Carbon Monoxide",
                "type": "text",
                "required": False,
            },
            {
                "name": "iaqTemperature",
                "label": "Temperature",
                "type": "text",
                "required": False,
            },
            {
                "name": "vocLevels",
                "label": "VOC Levels",
                "type": "text",
                "required": False,
            },
            {
                "name": "pm25",
                "label": "PM 2.5",
                "type": "text",
                "required": False,
            },
            {
                "name": "pm10",
                "label": "PM10",
                "type": "text",
                "required": False,
            },
            {
                "name": "moldOrMildew",
                "label": "Mold / Mildew",
                "type": "select",
                "required": False,
                "options": DIAGNOSTIC_YES_NO_OPTIONS,
            },
            {
                "name": "odors",
                "label": "Odors",
                "type": "select",
                "required": False,
                "options": DIAGNOSTIC_YES_NO_OPTIONS,
            },
        ),
    ),
)

HVAC_DIAGNOSTIC_FIELDS = tuple(
    (field["name"], field["label"])
    for _section_label, fields in HVAC_DIAGNOSTIC_SECTIONS
    for field in fields
)

HVAC_DIAGNOSTIC_FIELD_TYPES = {
    field["name"]: field.get("type", "text")
    for _section_label, fields in HVAC_DIAGNOSTIC_SECTIONS
    for field in fields
}

HVAC_FORM_PREFIX_BY_COLLECTION = {
    "airHandlers": "air_handler",
    "condensers": "condenser",
    "packageUnits": "unit",
    "miniSplits": "unit",
}


def _email_is_valid(email):
    return bool(EMAIL_PATTERN.match(email))


def _build_hvac_component(form_data, prefix):
    return {
        "model_name": form_data.get(f"{prefix}_model_name", "").strip(),
        "model_number": form_data.get(f"{prefix}_model_number", "").strip(),
        "serial_number": form_data.get(f"{prefix}_serial_number", "").strip(),
        "manufacturer": form_data.get(f"{prefix}_manufacturer", "").strip(),
        "install_year": form_data.get(f"{prefix}_install_year", "").strip(),
    }


def _build_hvac_ductwork(form_data):
    ductwork = {
        "type": form_data.get("ductwork_type", "").strip(),
        "insulated": form_data.get("ductwork_insulated", "").strip(),
        "supply_branches": form_data.get("ductwork_supply_branches", "").strip(),
        "returns": form_data.get("ductwork_returns", "").strip(),
        "notes_on_sizing": form_data.get("ductwork_notes_on_sizing", "").strip(),
        "install_year": form_data.get("ductwork_install_year", "").strip(),
    }
    return ductwork if any(ductwork.values()) else None


def _extract_hvac_ductwork(source):
    if not isinstance(source, dict):
        return None

    ductwork = source.get("ductwork")
    if not isinstance(ductwork, dict):
        return None

    normalized_ductwork = {
        "type": str(ductwork.get("type", "")).strip(),
        "insulated": str(ductwork.get("insulated", "")).strip(),
        "supply_branches": str(ductwork.get("supply_branches", "")).strip(),
        "returns": str(ductwork.get("returns", "")).strip(),
        "notes_on_sizing": str(ductwork.get("notes_on_sizing", "")).strip(),
        "install_year": str(ductwork.get("install_year", "")).strip(),
    }
    return normalized_ductwork if any(normalized_ductwork.values()) else None


def _build_hvac_system_document(customer_id, system_type, location_type, form_data):
    document = {
        "customer_id": reference_value(customer_id),
        "system_type": system_type,
        "location_type": location_type,
    }

    property_id = str(form_data.get("property_id") or "").strip()
    if property_id:
        document["property_id"] = reference_value(property_id)

    tonnage = str(form_data.get("tonnage", "")).strip()
    if tonnage:
        document["tonnage"] = tonnage

    if system_type in DUCTWORK_SYSTEM_TYPES:
        ductwork = _build_hvac_ductwork(form_data)
        if ductwork:
            document["ductwork"] = ductwork

    return document


def _get_missing_ductwork_fields(ductwork_data):
    ductwork_type = str(ductwork_data.get("type", "")).strip()
    if not ductwork_type:
        return []

    required_fields = ("insulated", "supply_branches", "returns", "install_year")
    return [
        field_name
        for field_name in required_fields
        if not str(ductwork_data.get(field_name, "")).strip()
    ]


def _validate_ductwork_data(ductwork_data):
    required_fields = {
        "insulated": "Insulated",
        "supply_branches": "Supply Branches",
        "returns": "Returns",
        "install_year": "Install Year",
    }
    missing_fields = [
        required_fields[field_name]
        for field_name in _get_missing_ductwork_fields(ductwork_data)
    ]
    if not missing_fields:
        return ""

    if len(missing_fields) == 1:
        return f"{missing_fields[0]} is required when a ductwork type is selected."

    if len(missing_fields) == 2:
        missing_text = f"{missing_fields[0]} and {missing_fields[1]}"
    else:
        missing_text = ", ".join(missing_fields[:-1]) + f", and {missing_fields[-1]}"

    return f"{missing_text} are required when a ductwork type is selected."


def _build_empty_hvac_form_data():
    return {
        "system_type": "",
        "tonnage": "",
        "location_type": "",
        "air_handler_model_name": "",
        "air_handler_model_number": "",
        "air_handler_serial_number": "",
        "air_handler_manufacturer": "",
        "air_handler_install_year": "",
        "condenser_model_name": "",
        "condenser_model_number": "",
        "condenser_serial_number": "",
        "condenser_manufacturer": "",
        "condenser_install_year": "",
        "furnace_model_name": "",
        "furnace_model_number": "",
        "furnace_serial_number": "",
        "furnace_manufacturer": "",
        "furnace_install_year": "",
        "unit_model_name": "",
        "unit_model_number": "",
        "unit_serial_number": "",
        "unit_manufacturer": "",
        "unit_install_year": "",
        "ductwork_type": "",
        "ductwork_insulated": "",
        "ductwork_supply_branches": "",
        "ductwork_returns": "",
        "ductwork_notes_on_sizing": "",
        "ductwork_install_year": "",
    }


def _find_existing_hvac_component(db, customer_id, hvac_system, collection_name):
    system_id = str(hvac_system.get("_id", "")).strip()
    if not system_id:
        return None

    return db[collection_name].find_one(
        {"$and": [build_reference_filter("customer_id", customer_id), {"hvac_system_id": system_id}]},
        sort=[("_id", -1)],
    )


def _build_hvac_component_document(form_data, component_base_document, collection_name):
    prefix = HVAC_FORM_PREFIX_BY_COLLECTION[collection_name]
    return {
        **component_base_document,
        **_build_hvac_component(form_data, prefix),
    }


def _summarize_hvac_component(component):
    summary_parts = []
    manufacturer = str(component.get("manufacturer", "")).strip()
    model_name = str(component.get("model_name", "")).strip()

    if manufacturer:
        summary_parts.append(manufacturer)
    if model_name:
        summary_parts.append(model_name)

    if not summary_parts:
        return "No component details saved yet."

    return " | ".join(summary_parts)


def _summarize_ductwork(component):
    ductwork = _extract_hvac_ductwork(component)
    if not ductwork:
        return ""

    summary_parts = []
    ductwork_type = str(ductwork.get("type", "")).strip()

    if ductwork_type:
        summary_parts.append(ductwork_type)

    return " | ".join(summary_parts)


def _format_hvac_component_detail(component):
    return {
        "model_name": str(component.get("model_name", "")).strip() or "-",
        "model_number": str(component.get("model_number", "")).strip() or "-",
        "serial_number": str(component.get("serial_number", "")).strip() or "-",
        "manufacturer": str(component.get("manufacturer", "")).strip() or "-",
        "install_year": str(component.get("install_year", "")).strip() or "-",
    }


def _format_diagnostics_key(key):
    return str(key).replace("_", " ").strip().title()


def _calculate_design_cfm_from_tonnage(tonnage_value):
    tonnage_text = str(tonnage_value or "").strip()
    if not tonnage_text:
        return ""

    tonnage_match = re.match(r"^(\d+(?:\.\d+)?)", tonnage_text)
    if not tonnage_match:
        return ""

    tonnage_number = float(tonnage_match.group(1))
    design_cfm = tonnage_number * 400
    if design_cfm.is_integer():
        return str(int(design_cfm))

    return str(design_cfm)


def _build_hvac_diagnostics_entry(form_data, hvac_system=None):
    entry = {}
    for field_name, _label in HVAC_DIAGNOSTIC_FIELDS:
        entry[field_name] = str(form_data.get(field_name, "")).strip()

    if hvac_system is not None:
        entry["designCfm"] = _calculate_design_cfm_from_tonnage(hvac_system.get("tonnage", ""))
    entry["date_performed"] = datetime.now().strftime("%m/%d/%Y")
    return entry


def _parse_date_performed(value):
    date_text = str(value or "").strip()
    if not date_text:
        return None

    for date_format in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
        try:
            return datetime.strptime(date_text, date_format)
        except ValueError:
            continue

    return None


def _sort_diagnostics_by_date_desc(diagnostics):
    if isinstance(diagnostics, dict):
        diagnostics_entries = [diagnostics]
    elif isinstance(diagnostics, list):
        diagnostics_entries = [entry for entry in diagnostics if isinstance(entry, dict)]
    else:
        diagnostics_entries = []

    return sorted(
        diagnostics_entries,
        key=lambda entry: (_parse_date_performed(entry.get("date_performed")) is not None, _parse_date_performed(entry.get("date_performed")) or datetime.min),
        reverse=True,
    )


def _build_latest_diagnostics_card(diagnostics):
    sorted_diagnostics = _sort_diagnostics_by_date_desc(diagnostics)
    latest_diagnostics = sorted_diagnostics[0] if sorted_diagnostics else None

    if not latest_diagnostics:
        return None

    date_performed = str(latest_diagnostics.get("date_performed", "")).strip()
    values = {}
    results = []
    for field_name, field_label in HVAC_DIAGNOSTIC_FIELDS:
        raw_value = latest_diagnostics.get(field_name, "")
        if isinstance(raw_value, (dict, list)):
            value = json.dumps(raw_value)
        else:
            value = str(raw_value).strip()
        values[field_name] = value
        if not value:
            continue
        results.append({"label": field_label, "value": value})

    # Include additional diagnostics keys that may exist in Mongo but are not
    # part of the currently modeled section fields.
    for field_name, raw_value in latest_diagnostics.items():
        if field_name in values or field_name == "date_performed":
            continue
        if isinstance(raw_value, (dict, list)):
            value = json.dumps(raw_value)
        else:
            value = str(raw_value).strip()
        values[field_name] = value

    if not date_performed and not results:
        return None

    return {
        "diagnostic_index": 0,
        "date_performed": date_performed or "-",
        "values": values,
        "results": results,
    }


def _build_hvac_diagnostic_detail(hvac_system, diagnostic_index):
    sorted_diagnostics = _sort_diagnostics_by_date_desc(hvac_system.get("diagnostics", []))
    if diagnostic_index < 0 or diagnostic_index >= len(sorted_diagnostics):
        return None

    selected_diagnostic = sorted_diagnostics[diagnostic_index]
    date_performed = str(selected_diagnostic.get("date_performed", "")).strip() or "-"
    section_details = []

    for section_label, fields in HVAC_DIAGNOSTIC_SECTIONS:
        rows = []
        for field in fields:
            field_name = field["name"]
            raw_value = selected_diagnostic.get(field_name, "")
            value = str(raw_value).strip() if raw_value is not None else ""
            rows.append({
                "label": field["label"],
                "value": value or "-",
            })
        section_details.append({
            "label": section_label,
            "rows": rows,
        })

    return {
        "diagnostic_index": diagnostic_index,
        "date_performed": date_performed,
        "sections": section_details,
    }


def _build_hvac_card_component(component, label, collection_name):
    return {
        "label": label,
        "collection_name": collection_name,
        "component_key": collection_name,
        "component_id": str(component.get("_id", "")).strip(),
        "summary": _summarize_hvac_component(component),
        "details": _format_hvac_component_detail(component),
    }


def _build_hvac_ductwork_component(hvac_system):
    ductwork = _extract_hvac_ductwork(hvac_system)
    details = {
        "type": "-",
        "insulated": "-",
        "supply_branches": "-",
        "returns": "-",
        "notes_on_sizing": "-",
        "install_year": "-",
    }
    summary = "No ductwork details saved yet."

    if ductwork:
        details = {
            "type": ductwork.get("type", "").strip() or "-",
            "insulated": ductwork.get("insulated", "").strip() or "-",
            "supply_branches": ductwork.get("supply_branches", "").strip() or "-",
            "returns": ductwork.get("returns", "").strip() or "-",
            "notes_on_sizing": ductwork.get("notes_on_sizing", "").strip() or "-",
            "install_year": ductwork.get("install_year", "").strip() or "-",
        }
        summary = _summarize_ductwork(hvac_system) or summary

    return {
        "label": "Ductwork",
        "collection_name": "ductwork",
        "component_key": "ductwork",
        "component_id": "",
        "summary": summary,
        "details": details,
    }


def _build_hvac_component_view_payload(db, customer_id, reference_type, reference_id, component_key):
    if reference_type != "system":
        return None

    hvac_system = db.hvacSystems.find_one({"$and": [{"_id": object_id_or_404(reference_id)}, build_reference_filter("customer_id", customer_id)]})
    if not hvac_system:
        return None

    requested_property_id = str(request.args.get("property_id") or "").strip()
    if requested_property_id:
        hvac_property_id = str(hvac_system.get("property_id") or "").strip()
        if hvac_property_id != requested_property_id:
            return None

    serialized_system = serialize_doc(hvac_system)
    system_type = str(serialized_system.get("system_type", "")).strip()
    allowed_component_keys = _get_allowed_component_keys(system_type)
    if component_key not in allowed_component_keys:
        return None

    diagnostics = _build_latest_diagnostics_card(serialized_system.get("diagnostics", {}))

    if component_key == "ductwork":
        ductwork = _extract_hvac_ductwork(serialized_system) or {}
        details = {
            "type": str(ductwork.get("type", "")).strip() or "-",
            "insulated": str(ductwork.get("insulated", "")).strip() or "-",
            "supply_branches": str(ductwork.get("supply_branches", "")).strip() or "-",
            "returns": str(ductwork.get("returns", "")).strip() or "-",
            "notes_on_sizing": str(ductwork.get("notes_on_sizing", "")).strip() or "-",
            "install_year": str(ductwork.get("install_year", "")).strip() or "-",
        }
        component_label = "Ductwork"
    else:
        existing_component = _find_existing_hvac_component(db, customer_id, serialized_system, component_key)
        serialized_component = serialize_doc(existing_component) if existing_component else {}
        details = _format_hvac_component_detail(serialized_component)
        component_label = HVAC_COMPONENT_LABELS.get(component_key, "Component")

    return {
        "reference_type": "system",
        "reference_id": reference_id,
        "system_type": serialized_system.get("system_type", "HVAC System"),
        "location_type": serialized_system.get("location_type", "Location not set"),
        "component_key": component_key,
        "component_label": component_label,
        "details": details,
        "diagnostics": diagnostics,
    }


def _get_allowed_component_keys(system_type):
    component_keys = [
        collection_name
        for collection_name, _label in HVAC_COLLECTION_CONFIG.get(system_type, ())
    ]
    if system_type in DUCTWORK_SYSTEM_TYPES:
        component_keys.append("ductwork")
    return component_keys


def _load_hvac_components_for_system(db, customer_id, hvac_system):
    system_type = str(hvac_system.get("system_type", "")).strip()
    expected_components = HVAC_COLLECTION_CONFIG.get(system_type, ())
    components = []
    component_snapshots = hvac_system.get("components", {}) if isinstance(hvac_system.get("components"), dict) else {}

    for collection_name, label in expected_components:
        snapshot = component_snapshots.get(collection_name)
        if isinstance(snapshot, dict) and snapshot:
            components.append(_build_hvac_card_component(snapshot, label, collection_name))
            continue

        matching_component = db[collection_name].find_one(
            {
                "$and": [
                    build_reference_filter("customer_id", customer_id),
                    {"hvac_system_id": str(hvac_system.get("_id", "")).strip()},
                ]
            },
            sort=[("_id", -1)],
        )

        if matching_component:
            serialized_component = serialize_doc(matching_component)
            components.append(_build_hvac_card_component(serialized_component, label, collection_name))

    return components


def _build_hvac_detail_payload(db, customer_id, reference_type, reference_id):
    if reference_type != "system":
        return None

    hvac_system = db.hvacSystems.find_one({"$and": [{"_id": object_id_or_404(reference_id)}, build_reference_filter("customer_id", customer_id)]})
    if not hvac_system:
        return None

    requested_property_id = str(request.args.get("property_id") or "").strip()
    if requested_property_id:
        hvac_property_id = str(hvac_system.get("property_id") or "").strip()
        if hvac_property_id != requested_property_id:
            return None

    serialized_system = serialize_doc(hvac_system)
    components = _load_hvac_components_for_system(db, customer_id, serialized_system)
    diagnostics = _build_latest_diagnostics_card(serialized_system.get("diagnostics", {}))
    reports = []
    for report in serialized_system.get("reports", []):
        if not isinstance(report, dict):
            continue
        reports.append(
            {
                "report_number": str(report.get("report_number", "System Health Report")).strip() or "System Health Report",
                "file_path": str(report.get("file_path", "#")).strip() or "#",
                "date_generated": str(report.get("date_generated", "")).strip() or "-",
                "diagnostics_date_performed": str(report.get("diagnostics_date_performed", "")).strip() or "-",
            }
        )

    if serialized_system.get("system_type") in DUCTWORK_SYSTEM_TYPES:
        components.append(_build_hvac_ductwork_component(serialized_system))

    return {
        "reference_type": "system",
        "reference_id": reference_id,
        "title": f"{serialized_system.get('system_type', 'HVAC System')} - {serialized_system.get('location_type', 'Location not set')}",
        "system_type": serialized_system.get("system_type", "HVAC System"),
        "tonnage": str(serialized_system.get("tonnage", "")).strip() or "-",
        "location_type": serialized_system.get("location_type", "Location not set"),
        "components": components,
        "diagnostics": diagnostics,
        "reports": reports,
    }


def _build_hvac_system_cards(db, customer_id, property_id=None):
    base_systems = [
        serialize_doc(hvac_system)
        for hvac_system in db.hvacSystems.find(build_reference_filter("customer_id", customer_id)).sort([("_id", -1)])
    ]

    normalized_property_id = str(property_id or "").strip()
    if normalized_property_id:
        filtered_base_systems = []
        for base_system in base_systems:
            system_property_id = str(base_system.get("property_id") or "").strip()
            if system_property_id == normalized_property_id:
                filtered_base_systems.append(base_system)
        base_systems = filtered_base_systems

    hvac_cards = []

    for base_system in base_systems:
        system_type = str(base_system.get("system_type", "")).strip()
        location_type = str(base_system.get("location_type", "")).strip()
        loaded_components = _load_hvac_components_for_system(db, customer_id, base_system)
        card_ductwork_summary = _summarize_ductwork(base_system)

        hvac_cards.append(
            {
                "reference_type": "system",
                "reference_id": str(base_system.get("_id", "")).strip(),
                "system_type": system_type or "HVAC System",
                "location_type": location_type or "Location not set",
                "ductwork_summary": card_ductwork_summary,
                "components": [
                    {
                        "label": component["label"],
                        "summary": component["summary"],
                    }
                    for component in loaded_components
                ],
            }
        )

    return hvac_cards


@bp.route("/customers")
def customers():
    db = ensure_connection_or_500()
    customers_list = [
        serialize_doc(customer)
        for customer in db.customers.find().sort([("last_name", 1), ("first_name", 1)])
    ]
    return render_template("customers/customers.html", customers=customers_list)


@bp.route("/customers/export/csv")
def export_customers_csv():
    db = ensure_connection_or_500()
    business_id = _resolve_current_business_id(db)

    query = {"_id": None}
    if business_id:
        related_customer_ids = set()

        for raw_customer_id in db.jobs.distinct("customer_id", {"business_id": business_id}):
            if isinstance(raw_customer_id, ObjectId):
                related_customer_ids.add(raw_customer_id)
            elif isinstance(raw_customer_id, str) and ObjectId.is_valid(raw_customer_id):
                related_customer_ids.add(ObjectId(raw_customer_id))

        for raw_customer_id in db.estimates.distinct("customer_id", {"business_id": business_id}):
            if isinstance(raw_customer_id, ObjectId):
                related_customer_ids.add(raw_customer_id)
            elif isinstance(raw_customer_id, str) and ObjectId.is_valid(raw_customer_id):
                related_customer_ids.add(ObjectId(raw_customer_id))

        business_scoped_filters = [
            {"business": business_id},
            {"business": str(business_id)},
            {"business_id": business_id},
            {"business_id": str(business_id)},
        ]
        if related_customer_ids:
            business_scoped_filters.append({"_id": {"$in": list(related_customer_ids)}})

        query = {"$or": business_scoped_filters}

    customers_rows = list(db.customers.find(query).sort([("last_name", 1), ("first_name", 1)]))
    return build_csv_export_response(customers_rows, "customers_export.csv")


@bp.route("/customers/add", methods=["GET", "POST"])
def add_customer():
    db = ensure_connection_or_500()
    if request.method == "POST":
        form_data = request.form.to_dict()
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        company = request.form.get("company", "").strip()
        customer_type = _normalize_customer_type(request.form.get("customer_type", "Residential"))
        phone = request.form.get("phone", "").strip()
        email = request.form.get("email", "").strip()
        address_line_1 = request.form.get("address_line_1", "").strip()
        address_line_2 = request.form.get("address_line_2", "").strip()
        city = request.form.get("city", "").strip()
        state = request.form.get("state", "").strip().upper()
        zip_code = request.form.get("zip_code", "").strip()
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
            "customer_type": customer_type,
            "phone": phone,
            "email": email,
            "address_line_1": address_line_1,
            "address_line_2": address_line_2,
            "city": city,
            "state": state,
            "zip_code": zip_code,
            "properties": [
                {
                    "property_id": str(ObjectId()),
                    "property_name": "Primary Residence",
                    "property_type": "single_family_home",
                    "address_line_1": address_line_1,
                    "address_line_2": address_line_2,
                    "city": city,
                    "state": state,
                    "zip_code": zip_code,
                    "is_default": True,
                    "is_seed_primary_address": True,
                }
            ],
            "referral_source": referral_source,
            "customer_status": customer_status,
            "date_added": datetime.now().strftime("%m/%d/%Y"),
            "created_at": datetime.utcnow(),
            "account_number": f"ACC-{customer_count:05d}",
            "account_type": "Residential",
            "balance_due": "$0.00",
            "balance_due_amount": 0.0,
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
        zip_code = request.form.get("zip_code", "").strip()

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
            "zip_code": zip_code,
            "referral_source": request.form.get("referral_source", "").strip(),
            "customer_status": next_status,
        }

        address_fields = ("address_line_1", "address_line_2", "city", "state", "zip_code")
        address_changed = any(
            str(customer.get(field, "") or "").strip() != str(update_data.get(field, "") or "").strip()
            for field in address_fields
        )

        if address_changed:
            customer_properties = _get_customer_properties(customer)
            synced_properties = []
            for prop in customer_properties:
                synced_prop = dict(prop)
                if synced_prop.get("is_seed_primary_address"):
                    synced_prop["address_line_1"] = update_data["address_line_1"]
                    synced_prop["address_line_2"] = update_data["address_line_2"]
                    synced_prop["city"] = update_data["city"]
                    synced_prop["state"] = update_data["state"]
                    synced_prop["zip_code"] = update_data["zip_code"]
                synced_properties.append(synced_prop)
            update_data["properties"] = synced_properties

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

    related_jobs = list(db.jobs.find(build_reference_filter("customer_id", customerId), {"_id": 1}))
    related_job_ids = [str(job.get("_id")) for job in related_jobs]

    db.customers.delete_one({"_id": customer_oid})
    db.jobs.delete_many(build_reference_filter("customer_id", customerId))
    db.equipment.delete_many(build_reference_filter("customer_id", customerId))
    db.estimates.delete_many(build_reference_filter("customer_id", customerId))
    if related_job_ids:
        db.estimates.delete_many({"job_id": {"$in": related_job_ids}})
    current_app.logger.info("Customer deleted: id=%s by employee_id=%s", customerId, session.get("employee_id"))
    return redirect(url_for("customers.customers"))


@bp.route("/customers/<customerId>/properties/add", methods=["GET", "POST"])
def add_property(customerId):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return redirect(url_for("customers.customers"))

    customer_type = _normalize_customer_type(customer.get("customer_type", "Residential"))
    property_type_options = _get_property_type_options(customer_type)
    form_data = {
        "property_id": str(ObjectId()),
        "property_name": "",
        "property_type": property_type_options[0] if property_type_options else "",
        "property_type_other": "",
        "address_line_1": "",
        "address_line_2": "",
        "city": "",
        "state": "",
        "zip_code": "",
        "is_default": False,
        "is_seed_primary_address": False,
    }
    error = ""

    if request.method == "POST":
        submitted = request.form.to_dict()
        submitted["property_id"] = form_data["property_id"]
        form_data = _normalize_property_payload(submitted, customer_type)

        if not _property_payload_is_valid(form_data):
            error = "Property name, property type, address line 1, city, state, and zip code are required."
        else:
            customer_properties = _get_customer_properties(customer)
            form_data["is_default"] = False
            form_data["is_seed_primary_address"] = False
            customer_properties.append(form_data)
            db.customers.update_one(
                {"_id": ObjectId(customerId)},
                {"$set": {"properties": customer_properties}},
            )
            return redirect(url_for("customers.view_customer", customerId=customerId))

    return render_template(
        "customers/add_property.html",
        customerId=customerId,
        customer=serialize_doc(customer),
        customer_type=customer_type,
        property_type_options=property_type_options,
        property_type_labels=PROPERTY_TYPE_LABELS,
        form_data=form_data,
        error=error,
    )


@bp.route("/customers/<customerId>/properties/<propertyId>")
def view_property(customerId, propertyId):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return redirect(url_for("customers.customers"))

    customer_property = _find_customer_property(customer, propertyId)
    if not customer_property:
        return redirect(url_for("customers.view_customer", customerId=customerId))

    hvac_systems = _build_hvac_system_cards(db, customerId, propertyId)

    return render_template(
        "customers/view_property.html",
        customerId=customerId,
        customer=serialize_doc(customer),
        property=customer_property,
        propertyId=propertyId,
        hvac_systems=hvac_systems,
    )


@bp.route("/customers/<customerId>/properties/<propertyId>/update", methods=["GET", "POST"])
def update_property(customerId, propertyId):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return redirect(url_for("customers.customers"))

    existing_property = _find_customer_property(customer, propertyId)
    if not existing_property:
        return redirect(url_for("customers.view_customer", customerId=customerId))

    customer_type = _normalize_customer_type(customer.get("customer_type", "Residential"))
    property_type_options = _get_property_type_options(customer_type)
    raw_property_type = str(existing_property.get("property_type") or "").strip()
    form_data = dict(existing_property)
    if raw_property_type not in property_type_options:
        form_data["property_type"] = "other"
        form_data["property_type_other"] = raw_property_type
    else:
        form_data["property_type"] = raw_property_type
        form_data["property_type_other"] = str(existing_property.get("property_type_other") or "").strip()

    error = ""
    if request.method == "POST":
        submitted = request.form.to_dict()
        submitted["property_id"] = propertyId
        form_data = _normalize_property_payload(submitted, customer_type)
        form_data["is_default"] = bool(existing_property.get("is_default"))
        form_data["is_seed_primary_address"] = bool(existing_property.get("is_seed_primary_address"))

        if not _property_payload_is_valid(form_data):
            error = "Property name, property type, address line 1, city, state, and zip code are required."
        else:
            customer_properties = []
            for prop in _get_customer_properties(customer):
                if prop.get("property_id") == propertyId:
                    customer_properties.append(form_data)
                else:
                    customer_properties.append(prop)

            db.customers.update_one(
                {"_id": ObjectId(customerId)},
                {"$set": {"properties": customer_properties}},
            )
            return redirect(url_for("customers.view_property", customerId=customerId, propertyId=propertyId))

    return render_template(
        "customers/update_property.html",
        customerId=customerId,
        customer=serialize_doc(customer),
        property=existing_property,
        propertyId=propertyId,
        customer_type=customer_type,
        property_type_options=property_type_options,
        property_type_labels=PROPERTY_TYPE_LABELS,
        form_data=form_data,
        error=error,
    )


@bp.route("/customers/<customerId>/properties/<propertyId>/delete", methods=["POST"])
def delete_property(customerId, propertyId):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return redirect(url_for("customers.customers"))

    customer_properties = [
        prop
        for prop in _get_customer_properties(customer)
        if prop.get("property_id") != propertyId
    ]
    db.customers.update_one(
        {"_id": ObjectId(customerId)},
        {"$set": {"properties": customer_properties}},
    )

    db.hvacSystems.delete_many(
        {
            "$and": [
                build_reference_filter("customer_id", customerId),
                build_reference_filter("property_id", propertyId),
            ]
        }
    )

    return redirect(url_for("customers.view_customer", customerId=customerId))


@bp.route("/customers/<customerId>/properties/<propertyId>/default", methods=["POST"])
def set_default_property(customerId, propertyId):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return redirect(url_for("customers.customers"))

    customer_properties = _get_customer_properties(customer)
    property_exists = any(prop.get("property_id") == propertyId for prop in customer_properties)
    if not property_exists:
        return redirect(url_for("customers.view_customer", customerId=customerId))

    updated_properties = []
    for prop in customer_properties:
        updated_prop = dict(prop)
        updated_prop["is_default"] = prop.get("property_id") == propertyId
        updated_properties.append(updated_prop)

    db.customers.update_one(
        {"_id": ObjectId(customerId)},
        {"$set": {"properties": updated_properties}},
    )

    return redirect(url_for("customers.view_customer", customerId=customerId))


@bp.route("/customers/<customerId>")
def view_customer(customerId):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return redirect(url_for("customers.customers"))

    jobs_page_raw = request.args.get("jobs_page", "1")
    payments_page_raw = request.args.get("payments_page", "1")
    estimates_page_raw = request.args.get("estimates_page", "1")

    try:
        jobs_page = max(1, int(jobs_page_raw))
    except ValueError:
        jobs_page = 1

    try:
        payments_page = max(1, int(payments_page_raw))
    except ValueError:
        payments_page = 1

    try:
        estimates_page = max(1, int(estimates_page_raw))
    except ValueError:
        estimates_page = 1

    jobs_per_page = 5
    payments_per_page = 5
    estimates_per_page = 5

    customer_jobs_total = db.jobs.count_documents(build_reference_filter("customer_id", customerId))
    customer_jobs_total_pages = (customer_jobs_total + jobs_per_page - 1) // jobs_per_page
    if customer_jobs_total_pages == 0:
        jobs_page = 1
    elif jobs_page > customer_jobs_total_pages:
        jobs_page = customer_jobs_total_pages

    customer_payments_total = db.payments.count_documents(build_reference_filter("customer_id", customerId))
    customer_payments_total_pages = (customer_payments_total + payments_per_page - 1) // payments_per_page
    if customer_payments_total_pages == 0:
        payments_page = 1
    elif payments_page > customer_payments_total_pages:
        payments_page = customer_payments_total_pages

    customer_estimates_total = db.estimates.count_documents(build_reference_filter("customer_id", customerId))
    customer_estimates_total_pages = (customer_estimates_total + estimates_per_page - 1) // estimates_per_page
    if customer_estimates_total_pages == 0:
        estimates_page = 1
    elif estimates_page > customer_estimates_total_pages:
        estimates_page = customer_estimates_total_pages

    customer_pages = {
        "jobs": jobs_page,
        "payments": payments_page,
        "estimates": estimates_page,
    }

    jobs_skip = (jobs_page - 1) * jobs_per_page
    customer_jobs = [
        serialize_doc(job)
        for job in db.jobs.find(build_reference_filter("customer_id", customerId)).sort([("scheduled_date", -1), ("scheduled_time", -1)]).skip(jobs_skip).limit(jobs_per_page)
    ]

    payments_skip = (payments_page - 1) * payments_per_page
    customer_payments = [
        serialize_doc(payment)
        for payment in db.payments.find(build_reference_filter("customer_id", customerId)).sort([("date", -1), ("_id", -1)]).skip(payments_skip).limit(payments_per_page)
    ]

    estimates_skip = (estimates_page - 1) * estimates_per_page
    customer_estimates = [
        serialize_doc(estimate)
        for estimate in db.estimates.find(build_reference_filter("customer_id", customerId)).sort([("_id", -1)]).skip(estimates_skip).limit(estimates_per_page)
    ]

    for estimate in customer_estimates:
        estimate["status"] = str(estimate.get("status") or "Created")

    properties = _get_customer_properties(customer)

    return render_template(
        "customers/view_customer.html",
        customerId=customerId,
        customer=serialize_doc(customer),
        customer_pages=customer_pages,
        customer_jobs=customer_jobs,
        customer_jobs_total_pages=customer_jobs_total_pages,
        customer_payments=customer_payments,
        customer_payments_total_pages=customer_payments_total_pages,
        customer_estimates=customer_estimates,
        customer_estimates_total_pages=customer_estimates_total_pages,
        properties=properties,
    )


@bp.route("/customers/<customerId>/hvac/<reference_type>/<reference_id>")
def view_hvac_system(customerId, reference_type, reference_id):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return redirect(url_for("customers.customers"))

    hvac_system = _build_hvac_detail_payload(db, customerId, reference_type, reference_id)
    if not hvac_system:
        return redirect(url_for("customers.view_customer", customerId=customerId))

    property_id = str(request.args.get("property_id") or "").strip()

    return render_template(
        "equipment/view_hvac_system.html",
        customerId=customerId,
        customer=serialize_doc(customer),
        hvac_system=hvac_system,
        property_id=property_id,
    )


@bp.route("/customers/<customerId>/hvac/<reference_type>/<reference_id>/diagnostics/<int:diagnostic_index>")
def view_hvac_diagnostic(customerId, reference_type, reference_id, diagnostic_index):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return redirect(url_for("customers.customers"))
    property_id = str(request.args.get("property_id") or "").strip()
    if reference_type != "system":
        return redirect(url_for("customers.view_customer", customerId=customerId))

    hvac_system = db.hvacSystems.find_one({"$and": [{"_id": object_id_or_404(reference_id)}, build_reference_filter("customer_id", customerId)]})
    if not hvac_system:
        return redirect(url_for("customers.view_customer", customerId=customerId))

    if property_id:
        if str(hvac_system.get("property_id") or "").strip() != property_id:
            return redirect(url_for("customers.view_customer", customerId=customerId))

    serialized_system = serialize_doc(hvac_system)
    diagnostic_detail = _build_hvac_diagnostic_detail(serialized_system, diagnostic_index)
    if not diagnostic_detail:
        return redirect(
            url_for(
                "customers.view_hvac_system",
                customerId=customerId,
                reference_type=reference_type,
                reference_id=reference_id,
                property_id=property_id,
            )
        )

    return render_template(
        "equipment/view_hvac_diagnostic.html",
        customerId=customerId,
        customer=serialize_doc(customer),
        reference_type=reference_type,
        reference_id=reference_id,
        hvac_system=serialized_system,
        diagnostic=diagnostic_detail,
        property_id=property_id,
    )


@bp.route("/customers/<customerId>/hvac/<reference_type>/<reference_id>/diagnostics/add", methods=["GET", "POST"])
def add_hvac_diagnostics(customerId, reference_type, reference_id):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return redirect(url_for("customers.customers"))
    property_id = str(request.args.get("property_id") or "").strip()
    if reference_type != "system":
        return redirect(url_for("customers.view_customer", customerId=customerId))

    hvac_system = db.hvacSystems.find_one({"$and": [{"_id": object_id_or_404(reference_id)}, build_reference_filter("customer_id", customerId)]})
    if not hvac_system:
        return redirect(url_for("customers.view_customer", customerId=customerId))

    if property_id:
        if str(hvac_system.get("property_id") or "").strip() != property_id:
            return redirect(url_for("customers.view_customer", customerId=customerId))

    error = ""
    form_data = {
        field_name: ""
        for field_name, _label in HVAC_DIAGNOSTIC_FIELDS
    }
    form_data["designCfm"] = _calculate_design_cfm_from_tonnage(hvac_system.get("tonnage", ""))

    if request.method == "POST":
        form_data = {
            field_name: request.form.get(field_name, "").strip()
            for field_name, _label in HVAC_DIAGNOSTIC_FIELDS
        }
        form_data["designCfm"] = _calculate_design_cfm_from_tonnage(hvac_system.get("tonnage", ""))

        diagnostics_entry = _build_hvac_diagnostics_entry(form_data, hvac_system)
        existing_diagnostics = _sort_diagnostics_by_date_desc(hvac_system.get("diagnostics", []))
        diagnostics_history = _sort_diagnostics_by_date_desc([diagnostics_entry, *existing_diagnostics])

        db.hvacSystems.update_one(
            {"_id": hvac_system["_id"]},
            {"$set": {"diagnostics": diagnostics_history}},
        )

        return redirect(
            url_for(
                "customers.view_hvac_system",
                customerId=customerId,
                reference_type=reference_type,
                reference_id=reference_id,
                property_id=property_id,
            )
        )

    return render_template(
        "equipment/add_hvac_diagnostics.html",
        customerId=customerId,
        customer=serialize_doc(customer),
        reference_type=reference_type,
        reference_id=reference_id,
        hvac_system=serialize_doc(hvac_system),
        error=error,
        form_data=form_data,
        diagnostics_sections=HVAC_DIAGNOSTIC_SECTIONS,
        property_id=property_id,
    )


@bp.route("/customers/<customerId>/hvac/<reference_type>/<reference_id>/reports/generate", methods=["POST"])
def generate_hvac_system_report(customerId, reference_type, reference_id):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return redirect(url_for("customers.customers"))
    property_id = str(request.args.get("property_id") or "").strip()
    if reference_type != "system":
        return redirect(url_for("customers.view_customer", customerId=customerId))

    hvac_system = db.hvacSystems.find_one({"$and": [{"_id": object_id_or_404(reference_id)}, build_reference_filter("customer_id", customerId)]})
    if not hvac_system:
        return redirect(url_for("customers.view_customer", customerId=customerId))

    if property_id:
        if str(hvac_system.get("property_id") or "").strip() != property_id:
            return redirect(url_for("customers.view_customer", customerId=customerId))

    serialized_system = serialize_doc(hvac_system)
    diagnostics_list = serialized_system.get("diagnostics", [])
    sorted_diagnostics = _sort_diagnostics_by_date_desc(diagnostics_list)
    raw_diagnostics = sorted_diagnostics[0] if sorted_diagnostics else None
    diagnostics_card = _build_latest_diagnostics_card(sorted_diagnostics)
    if not diagnostics_card:
        return redirect(
            url_for(
                "customers.view_hvac_system",
                customerId=customerId,
                reference_type=reference_type,
                reference_id=reference_id,
                property_id=property_id,
            )
        )

    existing_reports = serialized_system.get("reports", [])
    reports_history = existing_reports if isinstance(existing_reports, list) else []
    report_number = f"RPT-{reference_id[:8].upper()}-{len(reports_history) + 1:02d}"

    report_path = generate_hvac_system_health_report(
        hvac_system_id=reference_id,
        customer=serialize_doc(customer),
        hvac_system=serialized_system,
        diagnostics_card=diagnostics_card,
        report_number=report_number,
        raw_diagnostics=raw_diagnostics,
    )
    filename = os.path.basename(report_path)
    report_item = {
        "report_number": report_number,
        "file_path": url_for("download_invoice", filename=filename),
        "date_generated": datetime.now().strftime("%m/%d/%Y"),
        "diagnostics_date_performed": diagnostics_card.get("date_performed", "-"),
    }

    db.hvacSystems.update_one(
        {"$and": [{"_id": hvac_system["_id"]}, build_reference_filter("customer_id", customerId)]},
        {"$set": {"reports": [report_item, *reports_history]}},
    )

    return redirect(
        url_for(
            "customers.view_hvac_system",
            customerId=customerId,
            reference_type=reference_type,
            reference_id=reference_id,
            property_id=property_id,
        )
    )


@bp.route("/customers/<customerId>/hvac/<reference_type>/<reference_id>/components/<component_key>")
def view_hvac_component(customerId, reference_type, reference_id, component_key):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return redirect(url_for("customers.customers"))

    component = _build_hvac_component_view_payload(db, customerId, reference_type, reference_id, component_key)
    if not component:
        return redirect(
            url_for(
                "customers.view_hvac_system",
                customerId=customerId,
                reference_type=reference_type,
                reference_id=reference_id,
            )
        )

    property_id = str(request.args.get("property_id") or "").strip()

    return render_template(
        "equipment/view_hvac_component.html",
        customerId=customerId,
        customer=serialize_doc(customer),
        component=component,
        property_id=property_id,
    )


@bp.route("/customers/<customerId>/hvac/<reference_type>/<reference_id>/components/<component_key>/update", methods=["GET", "POST"])
def update_hvac_component(customerId, reference_type, reference_id, component_key):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    property_id = str(request.args.get("property_id") or "").strip()
    if not customer:
        return redirect(url_for("customers.customers"))
    if reference_type != "system":
        return redirect(url_for("customers.view_customer", customerId=customerId))

    hvac_system = db.hvacSystems.find_one({"$and": [{"_id": object_id_or_404(reference_id)}, build_reference_filter("customer_id", customerId)]})
    if not hvac_system:
        return redirect(url_for("customers.view_customer", customerId=customerId))
    if property_id and str(hvac_system.get("property_id") or "").strip() != property_id:
        return redirect(url_for("customers.view_customer", customerId=customerId))

    serialized_system = serialize_doc(hvac_system)
    system_type = str(serialized_system.get("system_type", "")).strip()
    location_type = str(serialized_system.get("location_type", "")).strip()
    allowed_component_keys = _get_allowed_component_keys(system_type)
    if component_key not in allowed_component_keys:
        return redirect(
            url_for(
                "customers.view_hvac_system",
                customerId=customerId,
                reference_type="system",
                reference_id=reference_id,
                property_id=property_id,
            )
        )

    if component_key == "ductwork":
        ductwork = _extract_hvac_ductwork(serialized_system) or {}
        error = ""
        ductwork_error_fields = []
        form_data = {
            "type": ductwork.get("type", ""),
            "insulated": ductwork.get("insulated", ""),
            "supply_branches": ductwork.get("supply_branches", ""),
            "returns": ductwork.get("returns", ""),
            "notes_on_sizing": ductwork.get("notes_on_sizing", ""),
            "install_year": ductwork.get("install_year", ""),
        }

        if request.method == "POST":
            form_data = {
                "type": request.form.get("type", "").strip(),
                "insulated": request.form.get("insulated", "").strip(),
                "supply_branches": request.form.get("supply_branches", "").strip(),
                "returns": request.form.get("returns", "").strip(),
                "notes_on_sizing": request.form.get("notes_on_sizing", "").strip(),
                "install_year": request.form.get("install_year", "").strip(),
            }
            error = _validate_ductwork_data(form_data)
            ductwork_error_fields = _get_missing_ductwork_fields(form_data)
            if error:
                return render_template(
                    "equipment/update_hvac_component.html",
                    customerId=customerId,
                    customer=serialize_doc(customer),
                    hvac_system=serialized_system,
                    reference_type=reference_type,
                    reference_id=reference_id,
                    component_label="Ductwork",
                    component_key=component_key,
                    field_prefix="",
                    is_ductwork=True,
                    form_data=form_data,
                    ductwork_type_options=DUCTWORK_TYPE_OPTIONS,
                    insulated_options=INSULATED_OPTIONS,
                    ductwork_error_fields=ductwork_error_fields,
                    error=error,
                    property_id=property_id,
                )

            if any(form_data.values()):
                db.hvacSystems.update_one(
                    {"$and": [{"_id": hvac_system["_id"]}, build_reference_filter("customer_id", customerId)]},
                    {"$set": {"ductwork": form_data}},
                )
            else:
                db.hvacSystems.update_one(
                    {"$and": [{"_id": hvac_system["_id"]}, build_reference_filter("customer_id", customerId)]},
                    {"$unset": {"ductwork": ""}},
                )

            return redirect(
                url_for(
                    "customers.view_hvac_system",
                    customerId=customerId,
                    reference_type="system",
                    reference_id=reference_id,
                    property_id=property_id,
                )
            )

        return render_template(
            "equipment/update_hvac_component.html",
            customerId=customerId,
            customer=serialize_doc(customer),
            hvac_system=serialized_system,
            reference_type=reference_type,
            reference_id=reference_id,
            component_label="Ductwork",
            component_key=component_key,
            field_prefix="",
            is_ductwork=True,
            form_data=form_data,
            ductwork_type_options=DUCTWORK_TYPE_OPTIONS,
            insulated_options=INSULATED_OPTIONS,
            ductwork_error_fields=ductwork_error_fields,
            error=error,
            property_id=property_id,
        )

    existing_component = _find_existing_hvac_component(db, customerId, serialized_system, component_key)
    serialized_component = serialize_doc(existing_component) if existing_component else {}
    field_prefix = HVAC_COMPONENT_FIELD_BY_COLLECTION.get(component_key, "")
    form_data = {
        "model_name": str(serialized_component.get("model_name", "")).strip(),
        "model_number": str(serialized_component.get("model_number", "")).strip(),
        "serial_number": str(serialized_component.get("serial_number", "")).strip(),
            "manufacturer": str(serialized_component.get("manufacturer", "")).strip(),
        "install_year": str(serialized_component.get("install_year", "")).strip(),
    }

    if request.method == "POST":
        form_data = {
            "model_name": request.form.get("model_name", "").strip(),
            "model_number": request.form.get("model_number", "").strip(),
            "serial_number": request.form.get("serial_number", "").strip(),
            "manufacturer": request.form.get("manufacturer", "").strip(),
            "install_year": request.form.get("install_year", "").strip(),
        }
        component_document = {
            "customer_id": reference_value(customerId),
            "system_type": system_type,
            "location_type": location_type,
            "hvac_system_id": str(hvac_system.get("_id", "")).strip(),
            **form_data,
        }

        if existing_component:
            db[component_key].update_one(
                {"$and": [{"_id": existing_component["_id"]}, build_reference_filter("customer_id", customerId)]},
                {"$set": component_document},
            )
        else:
            db[component_key].insert_one(component_document)

        return redirect(
            url_for(
                "customers.view_hvac_system",
                customerId=customerId,
                reference_type="system",
                reference_id=reference_id,
                property_id=property_id,
            )
        )

    return render_template(
        "equipment/update_hvac_component.html",
        customerId=customerId,
        customer=serialize_doc(customer),
        hvac_system=serialized_system,
        reference_type=reference_type,
        reference_id=reference_id,
        component_label=HVAC_COMPONENT_LABELS.get(component_key, "Component"),
        component_key=component_key,
        field_prefix=field_prefix,
        is_ductwork=False,
        form_data=form_data,
        manufacturer_options=MANUFACTURER_OPTIONS,
        property_id=property_id,
    )


@bp.route("/customers/<customerId>/hvac/<reference_type>/<reference_id>/delete", methods=["POST"])
def delete_hvac_system(customerId, reference_type, reference_id):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    property_id = str(request.args.get("property_id") or "").strip()
    if not customer:
        return redirect(url_for("customers.customers"))
    if reference_type != "system":
        return redirect(url_for("customers.view_customer", customerId=customerId))

    hvac_system = db.hvacSystems.find_one({"$and": [{"_id": object_id_or_404(reference_id)}, build_reference_filter("customer_id", customerId)]})
    if not hvac_system:
        return redirect(url_for("customers.view_customer", customerId=customerId))
    if property_id and str(hvac_system.get("property_id") or "").strip() != property_id:
        return redirect(url_for("customers.view_customer", customerId=customerId))

    serialized_system = serialize_doc(hvac_system)
    for collection_name in HVAC_COMPONENT_LABELS:
        existing_component = _find_existing_hvac_component(db, customerId, serialized_system, collection_name)
        if existing_component:
            db[collection_name].delete_one({"$and": [{"_id": existing_component["_id"]}, build_reference_filter("customer_id", customerId)]})

    db.hvacSystems.delete_one({"$and": [{"_id": hvac_system["_id"]}, build_reference_filter("customer_id", customerId)]})

    if property_id:
        return redirect(url_for("customers.view_property", customerId=customerId, propertyId=property_id))
    return redirect(url_for("customers.view_customer", customerId=customerId))


@bp.route("/customers/<customerId>/equipment/add", methods=["GET", "POST"])
def add_equipment(customerId):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return redirect(url_for("customers.customers"))

    property_id = str(request.args.get("property_id") or request.form.get("property_id") or "").strip()
    customer_property = _find_customer_property(customer, property_id) if property_id else None
    if property_id and not customer_property:
        return redirect(url_for("customers.view_customer", customerId=customerId))

    error = ""
    ductwork_error_fields = []
    form_data = _build_empty_hvac_form_data()
    form_data["property_id"] = property_id
    if request.method == "POST":
        for field_name in form_data:
            form_data[field_name] = request.form.get(field_name, "").strip()

        system_type = form_data["system_type"]
        location_type = form_data["location_type"]

        if not system_type:
            error = "System type is required."
        elif location_type not in LOCATION_TYPE_OPTIONS:
            error = "Please select a valid location type."
        else:
            ductwork_data = {
                "type": form_data.get("ductwork_type", ""),
                "insulated": form_data.get("ductwork_insulated", ""),
                "supply_branches": form_data.get("ductwork_supply_branches", ""),
                "returns": form_data.get("ductwork_returns", ""),
                "notes_on_sizing": form_data.get("ductwork_notes_on_sizing", ""),
                "install_year": form_data.get("ductwork_install_year", ""),
            }
            error = _validate_ductwork_data(ductwork_data)
            ductwork_error_fields = _get_missing_ductwork_fields(ductwork_data)

        if not error:
            base_document = _build_hvac_system_document(customerId, system_type, location_type, form_data)

            inserted_hvac_system = db.hvacSystems.insert_one(base_document)
            hvac_system_id = str(inserted_hvac_system.inserted_id)
            component_base_document = {
                **base_document,
                "hvac_system_id": hvac_system_id,
            }

            if system_type in {"Split", "Heat Pump"}:
                air_handler_doc = _build_hvac_component_document(form_data, component_base_document, "airHandlers")
                condenser_doc = _build_hvac_component_document(form_data, component_base_document, "condensers")
                db.airHandlers.insert_one(
                    air_handler_doc
                )
                db.condensers.insert_one(
                    condenser_doc
                )
                db.hvacSystems.update_one(
                    {"_id": inserted_hvac_system.inserted_id},
                    {
                        "$set": {
                            "components": {
                                "airHandlers": air_handler_doc,
                                "condensers": condenser_doc,
                            }
                        }
                    },
                )

            if system_type == "Package":
                package_doc = _build_hvac_component_document(form_data, component_base_document, "packageUnits")
                db.packageUnits.insert_one(
                    package_doc
                )
                db.hvacSystems.update_one(
                    {"_id": inserted_hvac_system.inserted_id},
                    {"$set": {"components": {"packageUnits": package_doc}}},
                )

            if system_type == "Mini Split":
                mini_split_doc = _build_hvac_component_document(form_data, component_base_document, "miniSplits")
                db.miniSplits.insert_one(
                    mini_split_doc
                )
                db.hvacSystems.update_one(
                    {"_id": inserted_hvac_system.inserted_id},
                    {"$set": {"components": {"miniSplits": mini_split_doc}}},
                )

            if property_id:
                return redirect(url_for("customers.view_property", customerId=customerId, propertyId=property_id))
            return redirect(url_for("customers.view_customer", customerId=customerId))

    return render_template(
        "equipment/add_equipment.html",
        customerId=customerId,
        customer=serialize_doc(customer),
        error=error,
        form_action=url_for("customers.add_equipment", customerId=customerId, property_id=property_id),
        page_title="Add HVAC System",
        breadcrumb_current="Add HVAC System",
        submit_label="Save HVAC System",
        form_data=form_data,
        system_type_options=SYSTEM_TYPE_OPTIONS,
        tonnage_options=TONNAGE_OPTIONS,
        location_type_options=LOCATION_TYPE_OPTIONS,
        ductwork_type_options=DUCTWORK_TYPE_OPTIONS,
        insulated_options=INSULATED_OPTIONS,
        manufacturer_options=MANUFACTURER_OPTIONS,
        ductwork_error_fields=ductwork_error_fields,
        property_id=property_id,
        property=customer_property,
    )


@bp.route("/customers/<customerId>/equipment/<equipmentId>")
def view_equipment(customerId, equipmentId):
    db = ensure_connection_or_500()
    customer = db.customers.find_one({"_id": object_id_or_404(customerId)})
    if not customer:
        return redirect(url_for("customers.customers"))

    equipment = db.equipment.find_one({"$and": [{"_id": object_id_or_404(equipmentId)}, build_reference_filter("customer_id", customerId)]})
    if not equipment:
        return redirect(url_for("customers.view_customer", customerId=customerId))

    serialized_equipment = serialize_doc(equipment)
    equipment_part_names = [
        (part.get("name") or "").strip()
        for part in serialized_equipment.get("parts", [])
        if (part.get("name") or "").strip()
    ]
    part_lookup = {}

    business_id = _resolve_current_business_id(db)
    if equipment_part_names:
        part_query = {"part_name": {"$in": equipment_part_names}}
        if business_id:
            part_query["business_id"] = business_id
        matching_parts = [
            serialize_doc(part)
            for part in db.parts.find(part_query)
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
                "code": matched_part.get("part_code", "") if matched_part else part.get("code", ""),
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

    equipment = db.equipment.find_one({"$and": [{"_id": object_id_or_404(equipmentId)}, build_reference_filter("customer_id", customerId)]})
    if not equipment:
        return redirect(url_for("customers.view_customer", customerId=customerId))

    error = ""
    business_id = _resolve_current_business_id(db)
    part_query = {"business_id": business_id} if business_id else {"_id": None}
    part_docs = [serialize_doc(part) for part in db.parts.find(part_query).sort("part_name", 1)]
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

            db.equipment.update_one({"$and": [{"_id": ObjectId(equipmentId)}, build_reference_filter("customer_id", customerId)]}, {"$set": update_data})
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

    db.equipment.delete_one({"$and": [{"_id": object_id_or_404(equipmentId)}, build_reference_filter("customer_id", customerId)]})
    return redirect(url_for("customers.view_customer", customerId=customerId))
