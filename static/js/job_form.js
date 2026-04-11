(function () {
  const form = document.getElementById('create-job-form');
  const submitButton = document.getElementById('submit-button');

  // Get service catalog from form data attribute
  const servicesCatalog = form && form.dataset.servicesCatalog ? JSON.parse(form.dataset.servicesCatalog) : {};
  const partsCatalog = form && form.dataset.partsCatalog ? JSON.parse(form.dataset.partsCatalog) : {};
  const laborsCatalog = form && form.dataset.laborsCatalog ? JSON.parse(form.dataset.laborsCatalog) : {};
  const materialsCatalog = form && form.dataset.materialsCatalog ? JSON.parse(form.dataset.materialsCatalog) : {};
  const discountsCatalog = form && form.dataset.discountsCatalog ? JSON.parse(form.dataset.discountsCatalog) : {};
  const partsById = form && form.dataset.partsById ? JSON.parse(form.dataset.partsById) : {};

  // Track user interaction for specific fields
  let servicesTouched = false;
  let dateTouched = false;
  let timeTouched = false;
  let employeeTouched = false;

  const estimateField = document.getElementById('job-is-estimate');
  const dateField = document.getElementById('job-date');
  const timeField = document.getElementById('job-time');
  const clearDateTimeButton = document.getElementById('clear-date-time');
  const dateFieldContainer = dateField ? dateField.closest('.add-customer-form-field') : null;
  const timeFieldContainer = timeField ? timeField.closest('.add-customer-form-field') : null;
  const scheduleRequiredByForm = !(form && form.dataset.scheduleRequired === 'false');

  function isScheduledDateRequired() {
    if (!scheduleRequiredByForm) {
      return false;
    }
    return !estimateField || estimateField.value === 'no';
  }

  function syncScheduledDateVisibility() {
    if (!dateField || !dateFieldContainer || !timeField || !timeFieldContainer) {
      return;
    }

    if (!scheduleRequiredByForm) {
      dateFieldContainer.style.display = '';
      dateField.required = false;
      timeFieldContainer.style.display = '';
      timeField.required = false;
      return;
    }

    if (isScheduledDateRequired()) {
      dateFieldContainer.style.display = '';
      dateField.required = true;
      timeFieldContainer.style.display = '';
      timeField.required = true;
      return;
    }

    dateFieldContainer.style.display = 'none';
    dateField.required = false;
    dateField.value = '';
    dateTouched = false;
    timeFieldContainer.style.display = 'none';
    timeField.required = false;
    timeField.value = '';
    timeTouched = false;
  }

  // Validation function
  function validateForm() {
    let isValid = true;

    // Get all inputs and selects that are required
    const requiredFields = [
      'job-address-line-1',
      'job-city',
      'job-state',
      'job-date',
      'job-time',
      'job-assigned-employee'
    ];

    requiredFields.forEach(function (fieldId) {
      const field = document.getElementById(fieldId);
      const errorElement = document.getElementById('error-' + fieldId.replace('job-', ''));

      if (!field) return;
      if ((fieldId === 'job-date' || fieldId === 'job-time') && !isScheduledDateRequired()) {
        if (errorElement) errorElement.style.display = 'none';
        return;
      }

      const isFieldValid = field.value.trim() !== '';
      if (!isFieldValid) {
        isValid = false;
        if (errorElement) errorElement.style.display = 'block';
      } else {
        if (errorElement) errorElement.style.display = 'none';
      }
    });

    // Check if at least one service is selected (only show error if touched)
    const serviceSelects = document.querySelectorAll('select[id^="service-code-"]');
    let hasSelectedService = false;
    serviceSelects.forEach(function (select) {
      if (select.value.trim() !== '') {
        hasSelectedService = true;
      }
    });

    const servicesError = document.getElementById('error-services');
    const servicesRequired = isScheduledDateRequired();
    if (servicesRequired && !hasSelectedService) {
      isValid = false;
      if (servicesError) servicesError.style.display = servicesTouched ? 'block' : 'none';
    } else {
      if (servicesError) servicesError.style.display = 'none';
    }

    // Handle date field error visibility based on touch status
    const employeeField = document.getElementById('job-assigned-employee');
    const jobDetailsError = document.getElementById('error-job-details');
    
    let jobDetailsErrors = [];
    
    if (dateField && isScheduledDateRequired() && dateField.value.trim() === '' && dateTouched) {
      isValid = false;
      jobDetailsErrors.push('Scheduled Date is required');
    }

    if (timeField && isScheduledDateRequired() && timeField.value.trim() === '' && timeTouched) {
      isValid = false;
      jobDetailsErrors.push('Scheduled Time is required');
    }
    
    if (employeeField && employeeField.value.trim() === '' && employeeTouched) {
      isValid = false;
      jobDetailsErrors.push('Assigned Employee is required');
    }
    
    if (jobDetailsError) {
      if (jobDetailsErrors.length > 0) {
        jobDetailsError.textContent = jobDetailsErrors.join(' • ');
        jobDetailsError.style.display = 'block';
      } else {
        jobDetailsError.style.display = 'none';
      }
    }

    // Update submit button state
    submitButton.disabled = !isValid;

    return isValid;
  }

  // Function to update price and duration when service is selected
  function attachPriceUpdateListener(selectElement) {
    selectElement.addEventListener('change', function () {
      servicesTouched = true; // Mark services as touched
      const selectedService = this.value;
      const serviceIndex = this.id.split('-')[2]; // Extract index from id like 'service-code-1'
      const priceInput = document.getElementById('service-standard-price-' + serviceIndex);
      const durationInput = document.getElementById('service-estimated-hours-' + serviceIndex);
      const serviceDetails = selectedService ? servicesCatalog[selectedService] : null;
      
      if (priceInput) {
        if (serviceDetails && (serviceDetails.standard_price || serviceDetails.price)) {
          priceInput.value = serviceDetails.standard_price || serviceDetails.price;
        } else if (!selectedService) {
          priceInput.value = '$0.00';
        }
      }

      if (durationInput) {
        if (serviceDetails && serviceDetails.estimated_hours) {
          durationInput.value = serviceDetails.estimated_hours;
        } else if (!selectedService) {
          durationInput.value = '';
        }
      }

      const serviceRow = this.closest('.job-service-row');
      if (serviceRow) {
        updateRemoveButtonVisibility(serviceRow, 'service');
      }

      refreshRemoveButtons('service');

      autoPopulatePartsForService(selectedService);

      validateForm();
    });
  }
  const initialSelects = document.querySelectorAll('select[id^="service-code-"]');
  initialSelects.forEach(function (select) {
    attachPriceUpdateListener(select);
  });

  const addServiceButton = document.getElementById("add-job-service-button");
  const servicesList = document.getElementById("job-services-list");
  const serviceOptionsTemplate = document.getElementById("service-options-template");
  const addPartButton = document.getElementById("add-job-part-button");
  const partsList = document.getElementById("job-parts-list");
  const partOptionsTemplate = document.getElementById("part-options-template");
  const addLaborButton = document.getElementById("add-job-labor-button");
  const laborsList = document.getElementById("job-labors-list");
  const laborOptionsTemplate = document.getElementById("labor-options-template");
  const addMaterialButton = document.getElementById("add-job-material-button");
  const materialsList = document.getElementById("job-materials-list");
  const materialOptionsTemplate = document.getElementById("material-options-template");
  const materialUnitOptionsTemplate = document.getElementById("material-unit-options-template");
  const addDiscountButton = document.getElementById("add-job-discount-button");
  const discountsList = document.getElementById("job-discounts-list");
  const discountOptionsTemplate = document.getElementById("discount-options-template");

  function getNextRowIndex(listElement, rowSelector, datasetKey) {
    const rows = listElement ? listElement.querySelectorAll(rowSelector) : [];
    let maxIndex = 0;

    rows.forEach(function (row) {
      const parsed = Number.parseInt(row.dataset[datasetKey], 10);
      if (!Number.isNaN(parsed) && parsed > maxIndex) {
        maxIndex = parsed;
      }
    });

    return maxIndex + 1;
  }

  function getServiceSelect(rowElement) {
    return rowElement ? rowElement.querySelector('select[name="service_code[]"]') : null;
  }

  function getPartSelect(rowElement) {
    return rowElement ? rowElement.querySelector('select[name="part_code[]"]') : null;
  }

  function getLaborSelect(rowElement) {
    return rowElement ? rowElement.querySelector('select[name="labor_description[]"]') : null;
  }

  function getMaterialSelect(rowElement) {
    return rowElement ? rowElement.querySelector('select[name="material_name[]"]') : null;
  }

  function getDiscountSelect(rowElement) {
    return rowElement ? rowElement.querySelector('select[name="discount_name[]"]') : null;
  }

  function isServiceRowPopulated(rowElement) {
    const select = getServiceSelect(rowElement);
    return !!(select && select.value.trim() !== '');
  }

  function isPartRowPopulated(rowElement) {
    const select = getPartSelect(rowElement);
    return !!(select && select.value.trim() !== '');
  }

  function isLaborRowPopulated(rowElement) {
    const select = getLaborSelect(rowElement);
    return !!(select && select.value.trim() !== '');
  }

  function isMaterialRowPopulated(rowElement) {
    const select = getMaterialSelect(rowElement);
    return !!(select && select.value.trim() !== '');
  }

  function isDiscountRowPopulated(rowElement) {
    const select = getDiscountSelect(rowElement);
    return !!(select && select.value.trim() !== '');
  }

  function updateRemoveButtonVisibility(rowElement, rowType) {
    const removeWrap = rowElement ? rowElement.querySelector('.job-row-remove-wrap') : null;
    if (!removeWrap) {
      return;
    }

    const rowCollection = rowType === 'service'
      ? (servicesList ? servicesList.querySelectorAll('.job-service-row') : [])
      : rowType === 'part'
        ? (partsList ? partsList.querySelectorAll('.job-part-row') : [])
        : rowType === 'labor'
          ? (laborsList ? laborsList.querySelectorAll('.job-labor-row') : [])
          : rowType === 'material'
            ? (materialsList ? materialsList.querySelectorAll('.job-material-row') : [])
            : (discountsList ? discountsList.querySelectorAll('.job-discount-row') : []);
    const hasMultipleRows = rowCollection.length > 1;

    const isPopulated = rowType === 'service'
      ? isServiceRowPopulated(rowElement)
      : rowType === 'part'
        ? isPartRowPopulated(rowElement)
        : rowType === 'labor'
          ? isLaborRowPopulated(rowElement)
          : rowType === 'material'
            ? isMaterialRowPopulated(rowElement)
            : isDiscountRowPopulated(rowElement);

    removeWrap.style.display = (isPopulated || hasMultipleRows) ? '' : 'none';
  }

  function refreshRemoveButtons(rowType) {
    const listElement = rowType === 'service' ? servicesList : rowType === 'part' ? partsList : rowType === 'labor' ? laborsList : rowType === 'material' ? materialsList : discountsList;
    const selector = rowType === 'service' ? '.job-service-row' : rowType === 'part' ? '.job-part-row' : rowType === 'labor' ? '.job-labor-row' : rowType === 'material' ? '.job-material-row' : '.job-discount-row';

    if (!listElement) {
      return;
    }

    const rows = listElement.querySelectorAll(selector);
    rows.forEach(function (row) {
      updateRemoveButtonVisibility(row, rowType);
    });
  }

  function createServiceRow() {
    const nextIndex = getNextRowIndex(servicesList, '.job-service-row', 'serviceIndex');
    const row = document.createElement("div");
    row.className = "add-customer-form-row job-service-row";
    row.dataset.serviceIndex = String(nextIndex);

    const optionHtml = serviceOptionsTemplate.innerHTML;

    row.innerHTML =
      '<div class="add-customer-form-field">' +
        '<label for="service-code-' + nextIndex + '">Service</label>' +
        '<select id="service-code-' + nextIndex + '" name="service_code[]">' +
          '<option value="">-- Select a service --</option>' +
          optionHtml +
        '</select>' +
      '</div>' +
      '<div class="add-customer-form-field">' +
        '<label for="service-standard-price-' + nextIndex + '">Standard Price</label>' +
        '<input id="service-standard-price-' + nextIndex + '" name="service_standard_price[]" type="text" placeholder="$0.00" />' +
      '</div>' +
      '<div class="add-customer-form-field">' +
        '<label for="service-estimated-hours-' + nextIndex + '">Estimated Hours</label>' +
        '<input id="service-estimated-hours-' + nextIndex + '" name="service_estimated_hours[]" type="number" step="0.25" min="0" placeholder="0" />' +
      '</div>';

    const newSelect = row.querySelector('select');
    if (newSelect) {
      attachPriceUpdateListener(newSelect);
    }

    attachRowRemoveButton(row, 'service');
    updateRemoveButtonVisibility(row, 'service');
    return row;
  }

  function createPartRow() {
    const nextIndex = getNextRowIndex(partsList, '.job-part-row', 'partIndex');
    const row = document.createElement("div");
    row.className = "add-customer-form-row job-part-row";
    row.dataset.partIndex = String(nextIndex);

    const optionHtml = partOptionsTemplate.innerHTML;

    row.innerHTML =
      '<div class="add-customer-form-field">' +
        '<label for="part-code-' + nextIndex + '">Part</label>' +
        '<select id="part-code-' + nextIndex + '" name="part_code[]">' +
          '<option value="">-- Select a part --</option>' +
          optionHtml +
        '</select>' +
      '</div>' +
      '<div class="add-customer-form-field">' +
        '<label for="part-unit-cost-' + nextIndex + '">Unit Cost</label>' +
        '<input id="part-unit-cost-' + nextIndex + '" name="part_unit_cost[]" type="text" placeholder="$0.00" />' +
      '</div>';

    const newSelect = row.querySelector('select');
    if (newSelect) {
      attachPartPriceUpdateListener(newSelect);
    }

    attachRowRemoveButton(row, 'part');
    updateRemoveButtonVisibility(row, 'part');
    return row;
  }

  function createLaborRow() {
    const nextIndex = getNextRowIndex(laborsList, '.job-labor-row', 'laborIndex');
    const row = document.createElement("div");
    row.className = "add-customer-form-row job-labor-row";
    row.dataset.laborIndex = String(nextIndex);

    const optionHtml = laborOptionsTemplate.innerHTML;

    row.innerHTML =
      '<div class="add-customer-form-field">' +
        '<label for="labor-description-' + nextIndex + '">Labor</label>' +
        '<select id="labor-description-' + nextIndex + '" name="labor_description[]">' +
          '<option value="">-- Select labor --</option>' +
          optionHtml +
        '</select>' +
      '</div>' +
      '<div class="add-customer-form-field">' +
        '<label for="labor-hours-' + nextIndex + '">Hours</label>' +
        '<input id="labor-hours-' + nextIndex + '" name="labor_hours[]" type="number" step="0.25" min="0" placeholder="0" />' +
      '</div>' +
      '<div class="add-customer-form-field">' +
        '<label for="labor-hourly-rate-' + nextIndex + '">Hourly Rate</label>' +
        '<input id="labor-hourly-rate-' + nextIndex + '" name="labor_hourly_rate[]" type="text" placeholder="$0.00" />' +
      '</div>';

    const newSelect = row.querySelector('select');
    if (newSelect) {
      attachLaborDefaultsListener(newSelect);
    }

    attachRowRemoveButton(row, 'labor');
    updateRemoveButtonVisibility(row, 'labor');
    return row;
  }

  function createMaterialRow() {
    const nextIndex = getNextRowIndex(materialsList, '.job-material-row', 'materialIndex');
    const row = document.createElement("div");
    row.className = "add-customer-form-row job-material-row";
    row.dataset.materialIndex = String(nextIndex);

    const optionHtml = materialOptionsTemplate.innerHTML;
    const unitOptionHtml = materialUnitOptionsTemplate ? materialUnitOptionsTemplate.innerHTML : '';

    row.innerHTML =
      '<div class="add-customer-form-field">' +
        '<label for="material-name-' + nextIndex + '">Material</label>' +
        '<select id="material-name-' + nextIndex + '" name="material_name[]">' +
          '<option value="">-- Select material --</option>' +
          optionHtml +
        '</select>' +
      '</div>' +
      '<div class="add-customer-form-field">' +
        '<label for="material-quantity-used-' + nextIndex + '">Quantity Used</label>' +
        '<input id="material-quantity-used-' + nextIndex + '" name="material_quantity_used[]" type="number" step="0.25" min="0" placeholder="0" />' +
      '</div>' +
      '<div class="add-customer-form-field">' +
        '<label for="material-unit-of-measure-' + nextIndex + '">Unit</label>' +
        '<select id="material-unit-of-measure-' + nextIndex + '" name="material_unit_of_measure[]">' +
          unitOptionHtml +
        '</select>' +
      '</div>' +
      '<div class="add-customer-form-field">' +
        '<label for="material-price-' + nextIndex + '">Price</label>' +
        '<input id="material-price-' + nextIndex + '" name="material_price[]" type="text" placeholder="$0.00" />' +
      '</div>';

    const newSelect = row.querySelector('select');
    if (newSelect) {
      attachMaterialDefaultsListener(newSelect);
    }

    attachRowRemoveButton(row, 'material');
    updateRemoveButtonVisibility(row, 'material');
    return row;
  }

  function createDiscountRow() {
    const nextIndex = getNextRowIndex(discountsList, '.job-discount-row', 'discountIndex');
    const row = document.createElement("div");
    row.className = "add-customer-form-row job-discount-row";
    row.dataset.discountIndex = String(nextIndex);

    const optionHtml = discountOptionsTemplate.innerHTML;

    row.innerHTML =
      '<div class="add-customer-form-field">' +
        '<label for="discount-name-' + nextIndex + '">Discount</label>' +
        '<select id="discount-name-' + nextIndex + '" name="discount_name[]">' +
          '<option value="">-- Select discount --</option>' +
          optionHtml +
        '</select>' +
      '</div>' +
      '<div class="add-customer-form-field">' +
        '<label for="discount-percentage-' + nextIndex + '">Discount Percentage</label>' +
        '<input id="discount-percentage-' + nextIndex + '" name="discount_percentage[]" type="number" step="0.01" min="0" max="100" placeholder="0" />' +
      '</div>' +
      '<div class="add-customer-form-field">' +
        '<label for="discount-amount-' + nextIndex + '">Discount Amount</label>' +
        '<input id="discount-amount-' + nextIndex + '" name="discount_amount[]" type="text" placeholder="$0.00" />' +
      '</div>';

    const newSelect = row.querySelector('select');
    if (newSelect) {
      attachDiscountDefaultsListener(newSelect);
    }

    attachRowRemoveButton(row, 'discount');
    updateRemoveButtonVisibility(row, 'discount');
    return row;
  }

  function ensureAtLeastOneServiceRow() {
    if (!servicesList || !serviceOptionsTemplate) {
      return;
    }

    const rows = servicesList.querySelectorAll('.job-service-row');
    if (rows.length === 0) {
      servicesList.appendChild(createServiceRow());
    }
  }

  function ensureAtLeastOnePartRow() {
    if (!partsList || !partOptionsTemplate) {
      return;
    }

    const rows = partsList.querySelectorAll('.job-part-row');
    if (rows.length === 0) {
      partsList.appendChild(createPartRow());
    }
  }

  function ensureAtLeastOneLaborRow() {
    if (!laborsList || !laborOptionsTemplate) {
      return;
    }

    const rows = laborsList.querySelectorAll('.job-labor-row');
    if (rows.length === 0) {
      laborsList.appendChild(createLaborRow());
    }
  }

  function ensureAtLeastOneMaterialRow() {
    if (!materialsList || !materialOptionsTemplate) {
      return;
    }

    const rows = materialsList.querySelectorAll('.job-material-row');
    if (rows.length === 0) {
      materialsList.appendChild(createMaterialRow());
    }
  }

  function ensureAtLeastOneDiscountRow() {
    if (!discountsList || !discountOptionsTemplate) {
      return;
    }

    const rows = discountsList.querySelectorAll('.job-discount-row');
    if (rows.length === 0) {
      discountsList.appendChild(createDiscountRow());
    }
  }

  function attachRowRemoveButton(rowElement, rowType) {
    if (!rowElement || rowElement.querySelector('.job-row-remove-button')) {
      return;
    }

    const removeWrap = document.createElement('div');
    removeWrap.className = 'job-row-remove-wrap';

    const removeButton = document.createElement('button');
    removeButton.type = 'button';
    removeButton.className = 'job-row-remove-button';
    removeButton.textContent = rowType === 'service' ? 'Remove Service' : rowType === 'part' ? 'Remove Part' : rowType === 'labor' ? 'Remove Labor' : rowType === 'material' ? 'Remove Material' : 'Remove Discount';

    removeButton.addEventListener('click', function () {
      rowElement.remove();
      if (rowType === 'service') {
        servicesTouched = true;
        ensureAtLeastOneServiceRow();
        refreshRemoveButtons('service');
      } else if (rowType === 'part') {
        ensureAtLeastOnePartRow();
        refreshRemoveButtons('part');
      } else if (rowType === 'labor') {
        ensureAtLeastOneLaborRow();
        refreshRemoveButtons('labor');
      } else if (rowType === 'material') {
        ensureAtLeastOneMaterialRow();
        refreshRemoveButtons('material');
      } else {
        ensureAtLeastOneDiscountRow();
        refreshRemoveButtons('discount');
      }
      validateForm();
    });

    removeWrap.appendChild(removeButton);
    rowElement.appendChild(removeWrap);
    updateRemoveButtonVisibility(rowElement, rowType);
  }

  if (!servicesList || !serviceOptionsTemplate) {
    return;
  }

  const initialServiceRows = servicesList.querySelectorAll('.job-service-row');
  initialServiceRows.forEach(function (row) {
    attachRowRemoveButton(row, 'service');
  });

  ensureAtLeastOneServiceRow();
  refreshRemoveButtons('service');

  if (addServiceButton) {
    addServiceButton.addEventListener("click", function () {
      servicesList.appendChild(createServiceRow());
      refreshRemoveButtons('service');

      validateForm();
    });
  }

  function attachPartPriceUpdateListener(selectElement) {
    selectElement.addEventListener('change', function () {
      const selectedPart = this.value;
      const partIndex = this.id.split('-')[2];
      const priceInput = document.getElementById('part-unit-cost-' + partIndex);
      const partDetails = selectedPart ? partsCatalog[selectedPart] : null;

      if (priceInput) {
        if (partDetails && (partDetails.unit_cost || partDetails.price)) {
          priceInput.value = partDetails.unit_cost || partDetails.price;
        } else if (!selectedPart) {
          priceInput.value = '$0.00';
        }
      }

      const partRow = this.closest('.job-part-row');
      if (partRow) {
        updateRemoveButtonVisibility(partRow, 'part');
      }

      refreshRemoveButtons('part');

    });
  }

  const initialPartSelects = document.querySelectorAll('select[id^="part-code-"]');
  initialPartSelects.forEach(function (select) {
    attachPartPriceUpdateListener(select);
  });

  function autoPopulatePartsForService(serviceCode) {
    if (!partsList || !partOptionsTemplate) return;

    // Remove any previously auto-populated rows
    partsList.querySelectorAll('.job-part-row[data-auto-populated]').forEach(function (row) {
      row.remove();
    });

    const serviceDetails = serviceCode ? servicesCatalog[serviceCode] : null;
    const partIds = serviceDetails && serviceDetails.part_ids ? serviceDetails.part_ids : [];

    if (!partIds.length) {
      ensureAtLeastOnePartRow();
      refreshRemoveButtons('part');
      return;
    }

    // Collect currently selected part codes (manually added rows) to avoid duplicates
    const existingCodes = new Set();
    partsList.querySelectorAll('.job-part-row').forEach(function (row) {
      const sel = getPartSelect(row);
      if (sel && sel.value.trim()) existingCodes.add(sel.value.trim());
    });

    // Check if there is a single empty placeholder row that can be reused
    const currentRows = partsList.querySelectorAll('.job-part-row');
    const isOnlyEmptyRow = currentRows.length === 1 && !isPartRowPopulated(currentRows[0]);
    let reusedEmptyRow = false;

    partIds.forEach(function (partId) {
      const partCode = partsById[partId];
      if (!partCode || existingCodes.has(partCode)) return;
      existingCodes.add(partCode);

      let targetRow;
      if (isOnlyEmptyRow && !reusedEmptyRow) {
        targetRow = currentRows[0];
        reusedEmptyRow = true;
      } else {
        targetRow = createPartRow();
        partsList.appendChild(targetRow);
      }

      targetRow.dataset.autoPopulated = 'true';

      const sel = getPartSelect(targetRow);
      if (sel) {
        sel.value = partCode;
        const idx = sel.id.replace('part-code-', '');
        const costInput = document.getElementById('part-unit-cost-' + idx);
        const partDetails = partsCatalog[partCode];
        if (costInput && partDetails) {
          costInput.value = partDetails.unit_cost || partDetails.price || '';
        }
        updateRemoveButtonVisibility(targetRow, 'part');
      }
    });

    refreshRemoveButtons('part');
  }

  const initialPartRows = partsList ? partsList.querySelectorAll('.job-part-row') : [];
  initialPartRows.forEach(function (row) {
    attachRowRemoveButton(row, 'part');
  });

  ensureAtLeastOnePartRow();
  refreshRemoveButtons('part');

  if (addPartButton && partsList && partOptionsTemplate) {
    addPartButton.addEventListener("click", function () {
      partsList.appendChild(createPartRow());
      refreshRemoveButtons('part');

      validateForm();
    });
  }

  function attachLaborDefaultsListener(selectElement) {
    selectElement.addEventListener('change', function () {
      const selectedLabor = this.value;
      const laborIndex = this.id.split('-')[2];
      const hoursInput = document.getElementById('labor-hours-' + laborIndex);
      const rateInput = document.getElementById('labor-hourly-rate-' + laborIndex);
      const laborDetails = selectedLabor ? laborsCatalog[selectedLabor] : null;

      if (hoursInput) {
        if (laborDetails && laborDetails.default_hours) {
          hoursInput.value = laborDetails.default_hours;
        } else if (!selectedLabor) {
          hoursInput.value = '';
        }
      }

      if (rateInput) {
        if (laborDetails && laborDetails.hourly_rate) {
          rateInput.value = laborDetails.hourly_rate;
        } else if (!selectedLabor) {
          rateInput.value = '$0.00';
        }
      }

      const laborRow = this.closest('.job-labor-row');
      if (laborRow) {
        updateRemoveButtonVisibility(laborRow, 'labor');
      }

      refreshRemoveButtons('labor');
    });
  }

  const initialLaborSelects = document.querySelectorAll('select[id^="labor-description-"]');
  initialLaborSelects.forEach(function (select) {
    attachLaborDefaultsListener(select);
  });

  const initialLaborRows = laborsList ? laborsList.querySelectorAll('.job-labor-row') : [];
  initialLaborRows.forEach(function (row) {
    attachRowRemoveButton(row, 'labor');
  });

  ensureAtLeastOneLaborRow();
  refreshRemoveButtons('labor');

  if (addLaborButton && laborsList && laborOptionsTemplate) {
    addLaborButton.addEventListener("click", function () {
      laborsList.appendChild(createLaborRow());
      refreshRemoveButtons('labor');

      validateForm();
    });
  }

  function attachMaterialDefaultsListener(selectElement) {
    selectElement.addEventListener('change', function () {
      const selectedMaterial = this.value;
      const materialIndex = this.id.split('-')[2];
      const quantityInput = document.getElementById('material-quantity-used-' + materialIndex);
      const unitInput = document.getElementById('material-unit-of-measure-' + materialIndex);
      const priceInput = document.getElementById('material-price-' + materialIndex);
      const materialDetails = selectedMaterial ? materialsCatalog[selectedMaterial] : null;

      if (quantityInput) {
        if (materialDetails && materialDetails.default_quantity_used) {
          quantityInput.value = materialDetails.default_quantity_used;
        } else if (!selectedMaterial) {
          quantityInput.value = '';
        }
      }

      if (unitInput) {
        if (materialDetails && materialDetails.unit_of_measure) {
          unitInput.value = materialDetails.unit_of_measure;
        } else if (!selectedMaterial) {
          unitInput.value = '';
        }
      }

      if (priceInput) {
        if (materialDetails && materialDetails.price) {
          priceInput.value = materialDetails.price;
        } else if (!selectedMaterial) {
          priceInput.value = '$0.00';
        }
      }

      const materialRow = this.closest('.job-material-row');
      if (materialRow) {
        updateRemoveButtonVisibility(materialRow, 'material');
      }

      refreshRemoveButtons('material');
    });
  }

  const initialMaterialSelects = document.querySelectorAll('select[id^="material-name-"]');
  initialMaterialSelects.forEach(function (select) {
    attachMaterialDefaultsListener(select);
    if (select.value) {
      select.dispatchEvent(new Event('change'));
    }
  });

  const initialMaterialRows = materialsList ? materialsList.querySelectorAll('.job-material-row') : [];
  initialMaterialRows.forEach(function (row) {
    attachRowRemoveButton(row, 'material');
  });

  ensureAtLeastOneMaterialRow();
  refreshRemoveButtons('material');

  if (addMaterialButton && materialsList && materialOptionsTemplate) {
    addMaterialButton.addEventListener("click", function () {
      materialsList.appendChild(createMaterialRow());
      refreshRemoveButtons('material');

      validateForm();
    });
  }

  function attachDiscountDefaultsListener(selectElement) {
    selectElement.addEventListener('change', function () {
      const selectedDiscount = this.value;
      const discountIndex = this.id.split('-')[2];
      const percentageInput = document.getElementById('discount-percentage-' + discountIndex);
      const amountInput = document.getElementById('discount-amount-' + discountIndex);
      const discountDetails = selectedDiscount ? discountsCatalog[selectedDiscount] : null;

      if (percentageInput) {
        if (discountDetails && discountDetails.discount_percentage) {
          percentageInput.value = discountDetails.discount_percentage;
        } else if (!selectedDiscount) {
          percentageInput.value = '';
        }
      }

      if (amountInput) {
        if (discountDetails && discountDetails.discount_amount) {
          amountInput.value = discountDetails.discount_amount;
        } else if (!selectedDiscount) {
          amountInput.value = '$0.00';
        }
      }

      const discountRow = this.closest('.job-discount-row');
      if (discountRow) {
        updateRemoveButtonVisibility(discountRow, 'discount');
      }

      refreshRemoveButtons('discount');
    });
  }

  const initialDiscountSelects = document.querySelectorAll('select[id^="discount-name-"]');
  initialDiscountSelects.forEach(function (select) {
    attachDiscountDefaultsListener(select);
    if (select.value) {
      select.dispatchEvent(new Event('change'));
    }
  });

  const initialDiscountRows = discountsList ? discountsList.querySelectorAll('.job-discount-row') : [];
  initialDiscountRows.forEach(function (row) {
    attachRowRemoveButton(row, 'discount');
  });

  ensureAtLeastOneDiscountRow();
  refreshRemoveButtons('discount');

  if (addDiscountButton && discountsList && discountOptionsTemplate) {
    addDiscountButton.addEventListener("click", function () {
      discountsList.appendChild(createDiscountRow());
      refreshRemoveButtons('discount');

      validateForm();
    });
  }

  // Add event listeners to validate on input change
  const fieldsToValidate = [
    'job-address-line-1',
    'job-city',
    'job-state',
    'job-date',
    'job-time',
    'job-assigned-employee'
  ];

  fieldsToValidate.forEach(function (fieldId) {
    const field = document.getElementById(fieldId);
    if (field) {
      field.addEventListener('change', function () {
        if (fieldId === 'job-date') {
          dateTouched = true;
        } else if (fieldId === 'job-time') {
          timeTouched = true;
        } else if (fieldId === 'job-assigned-employee') {
          employeeTouched = true;
        }
        validateForm();
      });
      field.addEventListener('input', function () {
        if (fieldId === 'job-date') {
          dateTouched = true;
        } else if (fieldId === 'job-time') {
          timeTouched = true;
        } else if (fieldId === 'job-assigned-employee') {
          employeeTouched = true;
        }
        validateForm();
      });
    }
  });

  if (clearDateTimeButton) {
    clearDateTimeButton.addEventListener('click', function () {
      if (dateField) {
        dateField.value = '';
      }
      if (timeField) {
        timeField.value = '';
      }
      dateTouched = false;
      timeTouched = false;
      validateForm();
    });
  }

  // Validate on form submission
  form.addEventListener('submit', function (event) {
    if (!validateForm()) {
      event.preventDefault();
    }
  });

  // Initial validation
   if (estimateField) {
     estimateField.addEventListener('change', function () {
       syncScheduledDateVisibility();
       validateForm();
     });
   }
   
   // Initial validation
   syncScheduledDateVisibility();
   validateForm();
})();
