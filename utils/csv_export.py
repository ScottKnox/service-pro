import csv
import io
import json
from datetime import date, datetime

from bson import ObjectId
from flask import Response


def _serialize_csv_value(value):
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, default=str)
    return str(value)


def build_csv_export_response(rows, filename, excluded_fields=None, field_transformers=None):
    excluded_fields = set(excluded_fields or [])
    field_transformers = dict(field_transformers or {})
    header_fields = []

    for row in rows:
        for key in row.keys():
            if key in excluded_fields:
                continue
            if key not in header_fields:
                header_fields.append(key)

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=header_fields)

    if header_fields:
        writer.writeheader()

    for row in rows:
        writer.writerow(
            {
                key: _serialize_csv_value(
                    field_transformers[key](row.get(key)) if key in field_transformers else row.get(key)
                )
                for key in header_fields
            }
        )

    response = Response(output.getvalue(), mimetype="text/csv")
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response