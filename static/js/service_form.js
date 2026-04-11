(function () {
  var form = document.getElementById('create-service-form');
  var addPartButton = document.getElementById('add-service-part-button');
  var partsList = document.getElementById('service-parts-list');
  var partOptionsTemplate = document.getElementById('service-part-options-template');

  var partsCatalog = (function () {
    if (form && form.dataset.partsCatalog) {
      try { return JSON.parse(form.dataset.partsCatalog); } catch (e) {}
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

  function ensureAtLeastOneRow() {
    if (!partsList) { return; }
    if (partsList.querySelectorAll('.service-part-row').length === 0) {
      partsList.appendChild(createPartRow());
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
        '<input id="service-part-cost-' + idx + '" name="part_cost_display[]" type="text" placeholder="$0.00" readonly />' +
      '</div>';
    attachSelectListener(row.querySelector('select'));
    attachRemoveButton(row);
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
        if (costInput && details) {
          costInput.value = details.unit_cost || '';
        }
      }
    });
  }

  refreshRemoveButtons();

  if (addPartButton) {
    addPartButton.addEventListener('click', function () {
      if (!partsList) { return; }
      partsList.appendChild(createPartRow());
      refreshRemoveButtons();
    });
  }
}());
