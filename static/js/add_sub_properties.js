(function () {
  'use strict';

  var SESSION_STORAGE_KEY = 'addSubPropertiesState';

  var form = document.getElementById('add-sub-properties-form');
  var generateBtn = document.getElementById('generate-btn');
  var addToSubPropertiesBtn = document.getElementById('add-to-sub-properties-btn');
  var container = document.getElementById('sub-property-rows-container');
  var subPropertiesListTitle = document.getElementById('sub-properties-list-title');
  var defaultHvacLink = document.getElementById('add-default-hvac-btn');

  if (!form || !generateBtn || !container) {
    return;
  }

  // Restore saved state from sessionStorage (when returning from default HVAC page)
  (function restoreState() {
    var saved = null;
    try {
      var raw = sessionStorage.getItem(SESSION_STORAGE_KEY);
      if (raw) {
        saved = JSON.parse(raw);
        sessionStorage.removeItem(SESSION_STORAGE_KEY);
      }
    } catch (e) {
      return;
    }

    if (!saved) {
      return;
    }

    if (saved.num_sub_properties) {
      var numInput = document.getElementById('num-sub-properties');
      if (numInput) numInput.value = saved.num_sub_properties;
    }
    if (saved.unit_prefix) {
      var prefixInput = document.getElementById('unit-prefix');
      if (prefixInput) prefixInput.value = saved.unit_prefix;
    }

    if (saved.rows && Array.isArray(saved.rows) && saved.rows.length > 0) {
      renderRows(saved.rows);
    }
  })();

  // Pre-populate existing sub-properties from DB (when no session restore happened)
  (function loadExistingSubProperties() {
    if (container.children.length > 0) {
      return; // session restore already populated the list
    }
    var raw = form.getAttribute('data-existing-sub-properties');
    if (!raw) return;
    var existing = null;
    try {
      existing = JSON.parse(raw);
    } catch (e) {
      return;
    }
    if (!Array.isArray(existing) || existing.length === 0) return;
    var rows = existing.map(function (sp) {
      return {
        sub_property_id: sp.sub_property_id || sp.subPropertyId || '',
        unit_label: sp.unit_label || sp.unitLabel || '',
        address_line_1: sp.address_line_1 || sp.addressLine1 || '',
        address_line_2: sp.address_line_2 || sp.addressLine2 || '',
        city: sp.city || '',
        state: sp.state || '',
        zip_code: sp.zip_code || sp.zipCode || ''
      };
    });
    renderRows(rows);
  })();

  // Save state before navigating to HVAC page
  if (defaultHvacLink) {
    defaultHvacLink.addEventListener('click', function () {
      var state = captureState();
      try {
        sessionStorage.setItem(SESSION_STORAGE_KEY, JSON.stringify(state));
      } catch (e) {
        // sessionStorage not available; state will be lost
      }
    });
  }

  generateBtn.addEventListener('click', function () {
    generateSubProperties(false);
  });

  if (addToSubPropertiesBtn) {
    addToSubPropertiesBtn.addEventListener('click', function () {
      generateSubProperties(true);
    });
  }

  function generateSubProperties(append) {
    var numInput = document.getElementById('num-sub-properties');
    var prefixInput = document.getElementById('unit-prefix');

    var num = parseInt((numInput ? numInput.value : '') || '0', 10);
    if (!num || num < 1) {
      return;
    }
    num = Math.min(num, 200);

    var sameAddress = true;
    var prefix = (prefixInput ? prefixInput.value : '').trim();
    var hasPrefix = prefix !== '';

    var parentAddressLine1 = form.getAttribute('data-parent-address-line-1') || '';
    var parentAddressLine2 = form.getAttribute('data-parent-address-line-2') || '';
    var parentCity = form.getAttribute('data-parent-city') || '';
    var parentState = form.getAttribute('data-parent-state') || '';
    var parentZipCode = form.getAttribute('data-parent-zip-code') || '';

    // Preserve existing row data when replacing or appending.
    var existingRows = collectRowData();
    var startIndex = 0;

    var rows = append ? existingRows.slice() : [];
    for (var i = 0; i < num; i++) {
      var rowIndex = startIndex + i;
      var label = buildIncrementedUnitValue(prefix, rowIndex);
      var existing = append ? {} : (existingRows[i] || {});
      rows.push({
        unit_label: label,
        address_line_1: existing.address_line_1 !== undefined ? existing.address_line_1 : (sameAddress ? parentAddressLine1 : ''),
        // Only auto-populate Address Line 2 when a Unit Prefix is provided.
        address_line_2: hasPrefix ? label : '',
        city: existing.city !== undefined ? existing.city : (sameAddress ? parentCity : ''),
        state: existing.state !== undefined ? existing.state : (sameAddress ? parentState : ''),
        zip_code: existing.zip_code !== undefined ? existing.zip_code : (sameAddress ? parentZipCode : ''),
      });
    }

    renderRows(rows);
  }

  function renderRows(rows) {
    container.innerHTML = '';

    rows.forEach(function (row, index) {
      var div = document.createElement('div');
      div.className = 'sub-property-row';
      div.setAttribute('data-index', index);

      div.innerHTML =
        '<div class="sub-property-row-header">' +
          '<span class="sub-property-unit-label">' + escapeHtml(row.unit_label || '') + '</span>' +
        '</div>' +
        '<div class="add-customer-form-row">' +
          '<div class="add-customer-form-field">' +
            '<label>Address Line 1</label>' +
            '<input type="text" name="sp_address_line_1_' + index + '" data-sp-field="address_line_1" value="' + escapeHtml(row.address_line_1 || '') + '">' +
          '</div>' +
          '<div class="add-customer-form-field">' +
            '<label>Address Line 2</label>' +
            '<input type="text" name="sp_address_line_2_' + index + '" data-sp-field="address_line_2" value="' + escapeHtml(row.address_line_2 || '') + '">' +
          '</div>' +
        '</div>' +
        '<div class="add-customer-form-row">' +
          '<div class="add-customer-form-field">' +
            '<label>City</label>' +
            '<input type="text" name="sp_city_' + index + '" data-sp-field="city" value="' + escapeHtml(row.city || '') + '">' +
          '</div>' +
          '<div class="add-customer-form-field">' +
            '<label>State</label>' +
            '<input type="text" name="sp_state_' + index + '" data-sp-field="state" maxlength="2" value="' + escapeHtml(row.state || '') + '">' +
          '</div>' +
          '<div class="add-customer-form-field">' +
            '<label>Zip Code</label>' +
            '<input type="text" name="sp_zip_code_' + index + '" data-sp-field="zip_code" value="' + escapeHtml(row.zip_code || '') + '">' +
          '</div>' +
        '</div>' +
        '<input type="hidden" data-sp-field="unit_label" value="' + escapeHtml(row.unit_label || '') + '">' +
        '<input type="hidden" data-sp-field="sub_property_id" value="' + escapeHtml(row.sub_property_id || '') + '">';

      container.appendChild(div);
    });

    updateSubPropertiesTitleVisibility(rows);
  }

  function updateSubPropertiesTitleVisibility(rows) {
    if (!subPropertiesListTitle) {
      return;
    }
    subPropertiesListTitle.hidden = !rows || rows.length === 0;
  }

  function collectRowData() {
    var rows = container.querySelectorAll('.sub-property-row');
    var result = [];
    rows.forEach(function (row) {
      var data = {};
      row.querySelectorAll('[data-sp-field]').forEach(function (el) {
        data[el.getAttribute('data-sp-field')] = el.value || '';
      });
      result.push(data);
    });
    return result;
  }

  function captureState() {
    var numInput = document.getElementById('num-sub-properties');
    var prefixInput = document.getElementById('unit-prefix');

    return {
      num_sub_properties: numInput ? numInput.value : '',
      unit_prefix: prefixInput ? prefixInput.value : '',
      rows: collectRowData(),
    };
  }

  function buildIncrementedUnitValue(prefix, index) {
    var trimmed = String(prefix || '').trim();
    if (!trimmed) {
      return String(index + 1);
    }

    var numericMatch = trimmed.match(/(\d+)/);
    if (!numericMatch) {
      return trimmed + (index + 1);
    }

    var numberText = numericMatch[1];
    var startNumber = parseInt(numberText, 10);
    if (!isFinite(startNumber)) {
      return trimmed + (index + 1);
    }

    var incremented = String(startNumber + index).padStart(numberText.length, '0');
    return trimmed.replace(numberText, incremented);
  }

  // On form submit, serialize all row data into the hidden JSON field
  form.addEventListener('submit', function () {
    var rows = collectRowData();
    // Use a dedicated hidden input for sub_properties_json (not the hvac one)
    var spInput = document.getElementById('sub-properties-json');
    if (!spInput) {
      spInput = document.createElement('input');
      spInput.type = 'hidden';
      spInput.name = 'sub_properties_json';
      spInput.id = 'sub-properties-json';
      form.appendChild(spInput);
    }
    spInput.value = JSON.stringify(rows);
  });

  function escapeHtml(str) {
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }
})();
