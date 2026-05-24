import pytest
import sys
import types

# Stub object storage for import side effects
def setup_module(module):
    stub_module = types.ModuleType("utils.object_storage")
    stub_module.download_object_bytes = lambda *args, **kwargs: b""
    sys.modules["utils.object_storage"] = stub_module

def test_pricing_summary_fallback():
    from blueprints.jobs import _build_pricing_summary
    payload = {
        "services": [
            {"standard_price": "$120.00"},
            {"price": "$80.00"},
        ],
        "parts": [
            {"sell_price": "$50.00"},
            {"unit_cost": "$25.00"},
        ],
        "labors": [{"line_total": "$30.00"}],
        "materials": [{"line_total": "$10.00"}],
        "equipments": [{"line_total": "$40.00"}],
        "discounts": [],
    }
    summary = _build_pricing_summary(payload)
    assert summary["subtotal"] == 355.0
    assert summary["total_due"] == 355.0
    assert summary["subtotal_display"] == "$355.00"
    assert summary["total_due_display"] == "$355.00"
