(function () {
  const form = document.getElementById('create-job-form');
  const submitButton = document.getElementById('submit-button');

  // Get service catalog from form data attribute
  const servicesCatalog = form && form.dataset.servicesCatalog ? JSON.parse(form.dataset.servicesCatalog) : {};
  const partsCatalog = form && form.dataset.partsCatalog ? JSON.parse(form.dataset.partsCatalog) : {};

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
    const serviceSelects = document.querySelectorAll('select[id^="service-type-"]');
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
      const serviceIndex = this.id.split('-')[2]; // Extract index from id like 'service-type-1'
      const priceInput = document.getElementById('service-price-' + serviceIndex);
      const durationInput = document.getElementById('service-duration-' + serviceIndex);
      const serviceDetails = selectedService ? servicesCatalog[selectedService] : null;
      
      if (priceInput) {
        if (serviceDetails && serviceDetails.price) {
          priceInput.value = serviceDetails.price;
        } else if (!selectedService) {
          priceInput.value = '$0.00';
        }
      }

      if (durationInput) {
        if (serviceDetails && serviceDetails.duration) {
          durationInput.value = serviceDetails.duration;
        } else if (!selectedService) {
          durationInput.value = '';
        }
      }

      const serviceRow = this.closest('.job-service-row');
      if (serviceRow) {
        updateRemoveButtonVisibility(serviceRow, 'service');
      }

      refreshRemoveButtons('service');

      validateForm();
    });
  }

  // Attach listeners to initial service selects
  const initialSelects = document.querySelectorAll('select[id^="service-type-"]');
  initialSelects.forEach(function (select) {
    attachPriceUpdateListener(select);
  });

  const addServiceButton = document.getElementById("add-job-service-button");
  const servicesList = document.getElementById("job-services-list");
  const serviceOptionsTemplate = document.getElementById("service-options-template");
  const addPartButton = document.getElementById("add-job-part-button");
  const partsList = document.getElementById("job-parts-list");
  const partOptionsTemplate = document.getElementById("part-options-template");

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
    return rowElement ? rowElement.querySelector('select[name="service_type[]"]') : null;
  }

  function getPartSelect(rowElement) {
    return rowElement ? rowElement.querySelector('select[name="part_name[]"]') : null;
  }

  function isServiceRowPopulated(rowElement) {
    const select = getServiceSelect(rowElement);
    return !!(select && select.value.trim() !== '');
  }

  function isPartRowPopulated(rowElement) {
    const select = getPartSelect(rowElement);
    return !!(select && select.value.trim() !== '');
  }

  function updateRemoveButtonVisibility(rowElement, rowType) {
    const removeWrap = rowElement ? rowElement.querySelector('.job-row-remove-wrap') : null;
    if (!removeWrap) {
      return;
    }

    const rowCollection = rowType === 'service'
      ? (servicesList ? servicesList.querySelectorAll('.job-service-row') : [])
      : (partsList ? partsList.querySelectorAll('.job-part-row') : []);
    const hasMultipleRows = rowCollection.length > 1;

    const isPopulated = rowType === 'service'
      ? isServiceRowPopulated(rowElement)
      : isPartRowPopulated(rowElement);

    removeWrap.style.display = (isPopulated || hasMultipleRows) ? '' : 'none';
  }

  function refreshRemoveButtons(rowType) {
    const listElement = rowType === 'service' ? servicesList : partsList;
    const selector = rowType === 'service' ? '.job-service-row' : '.job-part-row';

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
        '<label for="service-type-' + nextIndex + '">Service Type</label>' +
        '<select id="service-type-' + nextIndex + '" name="service_type[]">' +
          '<option value="">-- Select a service --</option>' +
          optionHtml +
        '</select>' +
      '</div>' +
      '<div class="add-customer-form-field">' +
        '<label for="service-price-' + nextIndex + '">Price</label>' +
        '<input id="service-price-' + nextIndex + '" name="service_price[]" type="text" placeholder="$0.00" />' +
      '</div>' +
      '<div class="add-customer-form-field">' +
        '<label for="service-duration-' + nextIndex + '">Duration</label>' +
        '<input id="service-duration-' + nextIndex + '" name="service_duration[]" type="text" placeholder="e.g. 2 hours" />' +
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
        '<label for="part-name-' + nextIndex + '">Part</label>' +
        '<select id="part-name-' + nextIndex + '" name="part_name[]">' +
          '<option value="">-- Select a part --</option>' +
          optionHtml +
        '</select>' +
      '</div>' +
      '<div class="add-customer-form-field">' +
        '<label for="part-price-' + nextIndex + '">Price</label>' +
        '<input id="part-price-' + nextIndex + '" name="part_price[]" type="text" placeholder="$0.00" />' +
      '</div>';

    const newSelect = row.querySelector('select');
    if (newSelect) {
      attachPartPriceUpdateListener(newSelect);
    }

    attachRowRemoveButton(row, 'part');
    updateRemoveButtonVisibility(row, 'part');
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

  function attachRowRemoveButton(rowElement, rowType) {
    if (!rowElement || rowElement.querySelector('.job-row-remove-button')) {
      return;
    }

    const removeWrap = document.createElement('div');
    removeWrap.className = 'job-row-remove-wrap';

    const removeButton = document.createElement('button');
    removeButton.type = 'button';
    removeButton.className = 'job-row-remove-button';
    removeButton.textContent = rowType === 'service' ? 'Remove Service' : 'Remove Part';

    removeButton.addEventListener('click', function () {
      rowElement.remove();
      if (rowType === 'service') {
        servicesTouched = true;
        ensureAtLeastOneServiceRow();
        refreshRemoveButtons('service');
      } else {
        ensureAtLeastOnePartRow();
        refreshRemoveButtons('part');
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
      const priceInput = document.getElementById('part-price-' + partIndex);
      const partDetails = selectedPart ? partsCatalog[selectedPart] : null;

      if (priceInput) {
        if (partDetails && partDetails.price) {
          priceInput.value = partDetails.price;
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

  const initialPartSelects = document.querySelectorAll('select[id^="part-name-"]');
  initialPartSelects.forEach(function (select) {
    attachPartPriceUpdateListener(select);
  });

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
