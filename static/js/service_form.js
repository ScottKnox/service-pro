(function () {
  var form = document.getElementById('create-service-form');
  var addPartButton = document.getElementById('add-service-part-button');
  var partsList = document.getElementById('service-parts-list');
  var partOptionsTemplate = document.getElementById('service-part-options-template');
  var addMaterialButton = document.getElementById('add-service-material-button');
  var materialsList = document.getElementById('service-materials-list');
  var materialOptionsTemplate = document.getElementById('service-material-options-template');
  var materialUnitOptionsTemplate = document.getElementById('service-material-unit-options-template');
  var addEquipmentButton = document.getElementById('add-service-equipment-button');
  var equipmentList = document.getElementById('service-equipment-list');
  var equipmentOptionsTemplate = document.getElementById('service-equipment-options-template');

  var partsCatalog = (function () {
    if (form && form.dataset.partsCatalog) {
      try { return JSON.parse(form.dataset.partsCatalog); } catch (e) {}
    }
    return {};
  }());

  var materialsCatalog = (function () {
    if (form && form.dataset.materialsCatalog) {
      try { return JSON.parse(form.dataset.materialsCatalog); } catch (e) {}
    }
    return {};
  }());

  function getNextIndex() {
    var rows = partsList ? partsList.querySelectorAll('.service-part-row') : [];
    var max = 0;
    rows.forEach(function (row) {
      var idx = parseInt(row.dataset.partIndex, 10);
      if (!isNaN(idx) && idx > max) { max = idx; }
    });
    return max + 1;
  }

  function getNextMaterialIndex() {
    var rows = materialsList ? materialsList.querySelectorAll('.service-material-row') : [];
    var max = 0;
    rows.forEach(function (row) {
      var idx = parseInt(row.dataset.materialIndex, 10);
      if (!isNaN(idx) && idx > max) { max = idx; }
    });
    return max + 1;
  }

  function refreshRemoveButtons() {
    if (!partsList) { return; }
    var rows = partsList.querySelectorAll('.service-part-row');
    rows.forEach(function (row) {
      var wrap = row.querySelector('.job-row-remove-wrap');
      if (!wrap) { return; }
      var select = row.querySelector('select');
      var populated = select && select.value.trim() !== '';
      wrap.style.display = (populated || rows.length > 1) ? '' : 'none';
    });
  }

  function refreshMaterialRemoveButtons() {
    if (!materialsList) { return; }
    var rows = materialsList.querySelectorAll('.service-material-row');
    rows.forEach(function (row) {
      var wrap = row.querySelector('.job-row-remove-wrap');
      if (!wrap) { return; }
      var select = row.querySelector('select');
      var populated = select && select.value.trim() !== '';
      wrap.style.display = (populated || rows.length > 1) ? '' : 'none';
    });
  }

  function attachSelectListener(select) {
    select.addEventListener('change', function () {
      var partId = this.value;
      var idx = this.id.replace('service-part-id-', '');
      var costInput = document.getElementById('service-part-cost-' + idx);
      var details = partId ? partsCatalog[partId] : null;
      if (costInput) {
        costInput.value = details ? (details.unit_cost || '') : '';
      }
      refreshRemoveButtons();
    });
  }

  function attachMaterialSelectListener(select) {
    select.addEventListener('change', function () {
      var materialId = this.value;
      var idx = this.id.replace('service-material-id-', '');
      var quantityInput = document.getElementById('service-material-quantity-used-' + idx);
      var unitInput = document.getElementById('service-material-unit-of-measure-' + idx);
      var priceInput = document.getElementById('service-material-price-' + idx);
      var details = materialId ? materialsCatalog[materialId] : null;
      if (quantityInput) {
        quantityInput.value = details ? (details.default_quantity_used || '') : '';
      }
      if (unitInput) {
        unitInput.value = details ? (details.unit_of_measure || '') : '';
      }
      if (priceInput) {
        priceInput.value = details ? (details.price || '') : '';
      }
      refreshMaterialRemoveButtons();
    });
  }

  function attachRemoveButton(row) {
    if (row.querySelector('.job-row-remove-button')) { return; }
    var wrap = document.createElement('div');
    wrap.className = 'job-row-remove-wrap';
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'job-row-remove-button';
    btn.textContent = 'Remove Part';
    btn.addEventListener('click', function () {
      row.remove();
      ensureAtLeastOneRow();
      refreshRemoveButtons();
    });
    wrap.appendChild(btn);
    row.appendChild(wrap);
  }

  function attachMaterialRemoveButton(row) {
    if (row.querySelector('.job-row-remove-button')) { return; }
    var wrap = document.createElement('div');
    wrap.className = 'job-row-remove-wrap';
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'job-row-remove-button';
    btn.textContent = 'Remove Material';
    btn.addEventListener('click', function () {
      row.remove();
      ensureAtLeastOneMaterialRow();
      refreshMaterialRemoveButtons();
    });
    wrap.appendChild(btn);
    row.appendChild(wrap);
  }

  function ensureAtLeastOneRow() {
    if (!partsList) { return; }
    if (partsList.querySelectorAll('.service-part-row').length === 0) {
      partsList.appendChild(createPartRow());
    }
  }

  function ensureAtLeastOneMaterialRow() {
    if (!materialsList) { return; }
    if (materialsList.querySelectorAll('.service-material-row').length === 0) {
      materialsList.appendChild(createMaterialRow());
    }
  }

  function createPartRow() {
    var idx = getNextIndex();
    var optionHtml = partOptionsTemplate ? partOptionsTemplate.innerHTML : '';
    var row = document.createElement('div');
    row.className = 'add-customer-form-row service-part-row';
    row.dataset.partIndex = String(idx);
    row.innerHTML =
      '<div class="add-customer-form-field">' +
        '<label for="service-part-id-' + idx + '">Part</label>' +
        '<select id="service-part-id-' + idx + '" name="part_id[]">' +
          '<option value="">-- Select a part --</option>' + optionHtml +
        '</select>' +
      '</div>' +
      '<div class="add-customer-form-field">' +
        '<label for="service-part-cost-' + idx + '">Unit Cost</label>' +
        '<input id="service-part-cost-' + idx + '" name="part_cost_display[]" type="text" placeholder="$0.00" />' +
      '</div>';
    attachSelectListener(row.querySelector('select'));
    attachRemoveButton(row);
    return row;
  }

  function createMaterialRow() {
    var idx = getNextMaterialIndex();
    var optionHtml = materialOptionsTemplate ? materialOptionsTemplate.innerHTML : '';
    var unitOptionHtml = materialUnitOptionsTemplate ? materialUnitOptionsTemplate.innerHTML : '';
    var row = document.createElement('div');
    row.className = 'add-customer-form-row service-material-row';
    row.dataset.materialIndex = String(idx);
    row.innerHTML =
      '<div class="add-customer-form-field">' +
        '<label for="service-material-id-' + idx + '">Material</label>' +
        '<select id="service-material-id-' + idx + '" name="material_id[]">' +
          '<option value="">-- Select a material --</option>' + optionHtml +
        '</select>' +
      '</div>' +
      '<div class="add-customer-form-field">' +
        '<label for="service-material-quantity-used-' + idx + '">Default Quantity Used</label>' +
        '<input id="service-material-quantity-used-' + idx + '" name="material_default_quantity_display[]" type="number" step="0.25" min="0" placeholder="0" />' +
      '</div>' +
      '<div class="add-customer-form-field">' +
        '<label for="service-material-unit-of-measure-' + idx + '">Unit of Measure</label>' +
        '<select id="service-material-unit-of-measure-' + idx + '" name="material_unit_of_measure_display[]">' + unitOptionHtml +
        '</select>' +
      '</div>' +
      '<div class="add-customer-form-field">' +
        '<label for="service-material-price-' + idx + '">Price</label>' +
        '<input id="service-material-price-' + idx + '" name="material_price_display[]" type="text" placeholder="$0.00" />' +
      '</div>';
    attachMaterialSelectListener(row.querySelector('select'));
    attachMaterialRemoveButton(row);
    return row;
  }

  // Wire up existing rows (handles update page pre-populated rows)
  if (partsList) {
    partsList.querySelectorAll('.service-part-row').forEach(function (row) {
      var select = row.querySelector('select');
      if (select) { attachSelectListener(select); }
      attachRemoveButton(row);
    });

    // Populate unit cost display for any pre-selected parts
    partsList.querySelectorAll('.service-part-row').forEach(function (row) {
      var select = row.querySelector('select');
      if (select && select.value) {
        var idx = select.id.replace('service-part-id-', '');
        var costInput = document.getElementById('service-part-cost-' + idx);
        var details = partsCatalog[select.value];
        if (costInput && details && !String(costInput.value || '').trim()) {
          costInput.value = details.unit_cost || '';
        }
      }
    });
  }

  if (materialsList) {
    materialsList.querySelectorAll('.service-material-row').forEach(function (row) {
      var select = row.querySelector('select');
      if (select) { attachMaterialSelectListener(select); }
      attachMaterialRemoveButton(row);
    });

    materialsList.querySelectorAll('.service-material-row').forEach(function (row) {
      var select = row.querySelector('select');
      if (select && select.value) {
        var idx = select.id.replace('service-material-id-', '');
        var quantityInput = document.getElementById('service-material-quantity-used-' + idx);
        var unitInput = document.getElementById('service-material-unit-of-measure-' + idx);
        var priceInput = document.getElementById('service-material-price-' + idx);
        var details = materialsCatalog[select.value];
        if (priceInput && details && !String(priceInput.value || '').trim()) {
          priceInput.value = details.price || '';
        }
        if (quantityInput && details && !String(quantityInput.value || '').trim()) {
          quantityInput.value = details.default_quantity_used || '';
        }
        if (unitInput && details && !String(unitInput.value || '').trim()) {
          unitInput.value = details.unit_of_measure || '';
        }
      }
    });
  }

  refreshRemoveButtons();
  refreshMaterialRemoveButtons();

  if (equipmentList) {
    equipmentList.querySelectorAll('.service-equipment-row').forEach(function (row) {
      attachEquipmentRemoveButton(row);
    });
    refreshEquipmentRemoveButtons();
  }

  if (addPartButton) {
    addPartButton.addEventListener('click', function () {
      if (!partsList) { return; }
      partsList.appendChild(createPartRow());
      refreshRemoveButtons();
    });
  }

  if (addMaterialButton) {
    addMaterialButton.addEventListener('click', function () {
      if (!materialsList) { return; }
      materialsList.appendChild(createMaterialRow());
      refreshMaterialRemoveButtons();
    });
  }

  if (addEquipmentButton) {
    addEquipmentButton.addEventListener('click', function () {
      if (!equipmentList) { return; }
      equipmentList.appendChild(createEquipmentRow());
      refreshEquipmentRemoveButtons();
    });
  }

  function getNextEquipmentIndex() {
    var rows = equipmentList ? equipmentList.querySelectorAll('.service-equipment-row') : [];
    var max = 0;
    rows.forEach(function (row) {
      var idx = parseInt(row.dataset.equipmentIndex, 10);
      if (!isNaN(idx) && idx > max) { max = idx; }
    });
    return max + 1;
  }

  function refreshEquipmentRemoveButtons() {
    if (!equipmentList) { return; }
    var rows = equipmentList.querySelectorAll('.service-equipment-row');
    rows.forEach(function (row) {
      var wrap = row.querySelector('.job-row-remove-wrap');
      if (!wrap) { return; }
      var select = row.querySelector('select');
      var populated = select && select.value.trim() !== '';
      wrap.style.display = (populated || rows.length > 1) ? '' : 'none';
    });
  }

  function attachEquipmentRemoveButton(row) {
    var wrap = row.querySelector('.job-row-remove-wrap');
    if (!wrap) {
      wrap = document.createElement('div');
      wrap.className = 'job-row-remove-wrap';
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'job-row-remove-button';
      btn.textContent = 'Remove';
      wrap.appendChild(btn);
      row.appendChild(wrap);
    }
    var btn = wrap.querySelector('.job-row-remove-button');
    if (btn) {
      btn.addEventListener('click', function () {
        row.remove();
        refreshEquipmentRemoveButtons();
      });
    }
  }

  function createEquipmentRow() {
    var idx = getNextEquipmentIndex();
    var optionHtml = equipmentOptionsTemplate ? equipmentOptionsTemplate.innerHTML : '';
    var row = document.createElement('div');
    row.className = 'add-customer-form-row service-equipment-row';
    row.dataset.equipmentIndex = String(idx);
    row.innerHTML =
      '<div class="add-customer-form-field">' +
        '<label for="service-equipment-id-' + idx + '">Equipment</label>' +
        '<select id="service-equipment-id-' + idx + '" name="equipment_id[]">' +
          '<option value="">-- Select equipment --</option>' + optionHtml +
        '</select>' +
      '</div>';
    attachEquipmentRemoveButton(row);
    return row;
  }
}());
