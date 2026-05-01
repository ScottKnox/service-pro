(function () {
  const form = document.getElementById('create-job-form');
  const submitButton = document.getElementById('submit-button');

  // Get service catalog from form data attribute
  const servicesCatalog = form && form.dataset.servicesCatalog ? JSON.parse(form.dataset.servicesCatalog) : {};
  const partsCatalog = form && form.dataset.partsCatalog ? JSON.parse(form.dataset.partsCatalog) : {};
  const laborsCatalog = form && form.dataset.laborsCatalog ? JSON.parse(form.dataset.laborsCatalog) : {};
  const materialsCatalog = form && form.dataset.materialsCatalog ? JSON.parse(form.dataset.materialsCatalog) : {};
  const equipmentsCatalog = form && form.dataset.equipmentsCatalog ? JSON.parse(form.dataset.equipmentsCatalog) : {};
  const discountsCatalog = form && form.dataset.discountsCatalog ? JSON.parse(form.dataset.discountsCatalog) : {};
  const partsById = form && form.dataset.partsById ? JSON.parse(form.dataset.partsById) : {};
  const materialsById = form && form.dataset.materialsById ? JSON.parse(form.dataset.materialsById) : {};
  const customerProperties = form && form.dataset.customerProperties ? JSON.parse(form.dataset.customerProperties) : [];

  const propertySelect = document.querySelector('[data-job-property-select]');
  const addressLine1Field = document.getElementById('job-address-line-1');
  const addressLine2Field = document.getElementById('job-address-line-2');
  const cityField = document.getElementById('job-city');
  const stateField = document.getElementById('job-state');
  const zipCodeField = document.getElementById('job-zip-code');

  const initialAddressValues = {
    address_line_1: addressLine1Field ? addressLine1Field.value : '',
    address_line_2: addressLine2Field ? addressLine2Field.value : '',
    city: cityField ? cityField.value : '',
    state: stateField ? stateField.value : '',
    zip_code: zipCodeField ? zipCodeField.value : '',
  };

  function applyPropertyAddress(propertyId) {
    if (!propertyId) {
      if (addressLine1Field) addressLine1Field.value = initialAddressValues.address_line_1;
      if (addressLine2Field) addressLine2Field.value = initialAddressValues.address_line_2;
      if (cityField) cityField.value = initialAddressValues.city;
      if (stateField) stateField.value = initialAddressValues.state;
      if (zipCodeField) zipCodeField.value = initialAddressValues.zip_code;
      return;
    }

    const selectedProperty = Array.isArray(customerProperties)
      ? customerProperties.find(function (property) {
          return String((property || {}).property_id || '').trim() === String(propertyId).trim();
        })
      : null;

    if (!selectedProperty) {
      return;
    }

    if (addressLine1Field) addressLine1Field.value = selectedProperty.address_line_1 || '';
    if (addressLine2Field) addressLine2Field.value = selectedProperty.address_line_2 || '';
    if (cityField) cityField.value = selectedProperty.city || '';
    if (stateField) stateField.value = selectedProperty.state || '';
    if (zipCodeField) zipCodeField.value = selectedProperty.zip_code || '';
  }

  if (propertySelect) {
    propertySelect.addEventListener('change', function () {
      applyPropertyAddress(this.value);
      validateForm();
    });

    if (propertySelect.value) {
      applyPropertyAddress(propertySelect.value);
    }
  }

  // Track user interaction for specific fields
  let servicesTouched = false;
  let dateTouched = false;
  let timeTouched = false;
  let employeeTouched = false;
  let submitAttempted = false;

  const estimateField = document.getElementById('job-is-estimate');
  const dateField = document.getElementById('job-date');
  const timeField = document.getElementById('job-time');
  const clearDateTimeButton = document.getElementById('clear-date-time') || document.getElementById('clear-proposed-date-time');
  const clearableDateField = dateField || document.getElementById('proposed-job-date');
  const clearableTimeField = timeField || document.getElementById('proposed-job-time');
  const dateFieldContainer = dateField ? dateField.closest('.add-customer-form-field') : null;
  const timeFieldContainer = timeField ? timeField.closest('.add-customer-form-field') : null;
  const scheduleTypeField = document.getElementById('job-schedule-type');
  const recurringSettings = document.getElementById('job-recurring-settings');
  const recurringFrequencyField = document.getElementById('recurring-frequency');
  const recurringEndTypeField = document.getElementById('recurring-end-type');
  const recurringEndTypeWrap = recurringEndTypeField ? recurringEndTypeField.closest('.add-customer-form-field') : null;
  const recurringEndDateField = document.getElementById('recurring-end-date');
  const recurringEndAfterField = document.getElementById('recurring-end-after');
  const recurringEndDateWrap = document.getElementById('recurring-end-date-wrap');
  const recurringEndAfterWrap = document.getElementById('recurring-end-after-wrap');
  const scheduleRequiredByForm = !(form && form.dataset.scheduleRequired === 'false');

  function isRecurringJob() {
    return !!(scheduleTypeField && scheduleTypeField.value === 'recurring');
  }

  function isScheduledDateRequired() {
    if (isRecurringJob()) {
      return true;
    }
    if (!scheduleRequiredByForm) {
      return false;
    }
    return !estimateField || estimateField.value === 'no';
  }

  function syncRecurringEndFields() {
    const recurringSelected = isRecurringJob();
    const endType = recurringEndTypeField ? recurringEndTypeField.value : 'never';
    const showEndDate = recurringSelected && endType === 'on_date';
    const showEndAfter = recurringSelected && endType === 'after_occurrences';

    if (recurringEndDateWrap) {
      recurringEndDateWrap.hidden = !showEndDate;
      recurringEndDateWrap.style.display = showEndDate ? '' : 'none';
    }
    if (recurringEndDateField) {
      recurringEndDateField.required = showEndDate;
      if (!showEndDate) {
        recurringEndDateField.value = '';
      }
    }

    if (recurringEndAfterWrap) {
      recurringEndAfterWrap.hidden = !showEndAfter;
      recurringEndAfterWrap.style.display = showEndAfter ? '' : 'none';
    }
    if (recurringEndAfterField) {
      recurringEndAfterField.required = showEndAfter;
      if (!showEndAfter) {
        recurringEndAfterField.value = '';
      }
    }

    if (recurringEndTypeField) {
      recurringEndTypeField.required = recurringSelected;
    }
  }

  function syncRecurringSettingsVisibility() {
    const recurringSelected = isRecurringJob();

    if (recurringSettings) {
      recurringSettings.hidden = !recurringSelected;
      recurringSettings.style.display = recurringSelected ? '' : 'none';
    }
    if (recurringEndTypeWrap) {
      recurringEndTypeWrap.hidden = !recurringSelected;
      recurringEndTypeWrap.style.display = recurringSelected ? '' : 'none';
    }
    if (recurringFrequencyField) {
      recurringFrequencyField.required = recurringSelected;
    }
    syncRecurringEndFields();
    syncScheduledDateVisibility();
  }

  function syncScheduledDateVisibility() {
    if (!dateField || !dateFieldContainer || !timeField || !timeFieldContainer) {
      return;
    }

    if (!scheduleRequiredByForm && !isRecurringJob()) {
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

  function setFieldError(errorId, isVisible, message) {
    const errorElement = document.getElementById(errorId);
    if (!errorElement) {
      return;
    }
    if (message) {
      errorElement.textContent = message;
    }
    errorElement.style.display = isVisible ? 'block' : 'none';
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
      const errorId = 'error-' + fieldId.replace('job-', '');

      if (!field) return;
      if ((fieldId === 'job-date' || fieldId === 'job-time') && !isScheduledDateRequired()) {
        setFieldError(errorId, false);
        return;
      }

      const isFieldValid = field.value.trim() !== '';
      if (!isFieldValid) {
        isValid = false;
        const shouldShow = fieldId === 'job-assigned-employee' || submitAttempted || (fieldId === 'job-date' && dateTouched) || (fieldId === 'job-time' && timeTouched);
        setFieldError(errorId, shouldShow);
      } else {
        setFieldError(errorId, false);
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

    const servicesErrorId = 'error-service-code';
    const servicesRequired = isScheduledDateRequired();
    if (servicesRequired && !hasSelectedService) {
      isValid = false;
      setFieldError(servicesErrorId, servicesTouched || submitAttempted);
    } else {
      setFieldError(servicesErrorId, false);
    }

    if (isRecurringJob()) {
      if (recurringFrequencyField && recurringFrequencyField.value.trim() === '') {
        isValid = false;
        setFieldError('error-recurring-frequency', true);
      } else {
        setFieldError('error-recurring-frequency', false);
      }

      if (recurringEndTypeField && recurringEndTypeField.value.trim() === '') {
        isValid = false;
        setFieldError('error-recurring-end-type', true);
      } else {
        setFieldError('error-recurring-end-type', false);
      }

      if (recurringEndTypeField && recurringEndTypeField.value === 'on_date' && recurringEndDateField && recurringEndDateField.value.trim() === '') {
        isValid = false;
        setFieldError('error-recurring-end-date', true);
      } else {
        setFieldError('error-recurring-end-date', false);
      }

      if (recurringEndTypeField && recurringEndTypeField.value === 'after_occurrences' && recurringEndAfterField && recurringEndAfterField.value.trim() === '') {
        isValid = false;
        setFieldError('error-recurring-end-after', true);
      } else {
        setFieldError('error-recurring-end-after', false);
      }
    } else {
      setFieldError('error-recurring-frequency', false);
      setFieldError('error-recurring-end-type', false);
      setFieldError('error-recurring-end-date', false);
      setFieldError('error-recurring-end-after', false);
    }

    // Update submit button state
    submitButton.disabled = !isValid;

    return isValid;
  }

  // Function to update price and duration when service is selected
  function isTruthyValue(value) {
    return ['1', 'true', 'yes', 'on'].indexOf(String(value || '').trim().toLowerCase()) !== -1;
  }

  function getServiceRowByIndex(serviceIndex) {
    return document.querySelector('.job-service-row[data-service-index="' + serviceIndex + '"]');
  }

  function getServicePriceInput(serviceIndex) {
    return document.getElementById('service-price-' + serviceIndex);
  }

  function getServiceHoursInput(serviceIndex) {
    return document.getElementById('service-hours-' + serviceIndex);
  }

  function getServiceEmergencyCheckbox(serviceIndex) {
    return document.getElementById('service-emergency-call-' + serviceIndex);
  }

  function getServiceEmergencyHiddenInput(serviceIndex) {
    return document.getElementById('service-emergency-call-hidden-' + serviceIndex);
  }

  function serviceSupportsEmergency(serviceDetails) {
    if (!serviceDetails) {
      return false;
    }

    if (typeof serviceDetails.emergency === 'boolean') {
      return serviceDetails.emergency;
    }

    return isTruthyValue(serviceDetails.emergency);
  }

  function getServiceEmergencyWarning(serviceIndex) {
    const checkbox = getServiceEmergencyCheckbox(serviceIndex);
    const fieldContainer = checkbox ? checkbox.closest('.add-customer-form-field') : null;
    if (!fieldContainer) {
      return null;
    }

    let warning = fieldContainer.querySelector('.service-emergency-warning');
    if (!warning) {
      warning = document.createElement('p');
      warning.className = 'form-field-note service-emergency-warning';
      warning.textContent = 'This service does not have emergency pricing enabled.';
      warning.style.display = 'none';
      warning.style.margin = '0.35rem 0 0';
      fieldContainer.appendChild(warning);
    }

    return warning;
  }

  function setEmergencyWarningVisible(serviceIndex, isVisible) {
    const warning = getServiceEmergencyWarning(serviceIndex);
    if (!warning) {
      return;
    }

    warning.style.display = isVisible ? 'block' : 'none';
  }

  function syncEmergencyAvailability(serviceIndex, showWarningWhenUnsupported) {
    const serviceSelect = document.getElementById('service-code-' + serviceIndex);
    const emergencyCheckbox = getServiceEmergencyCheckbox(serviceIndex);
    const hiddenInput = getServiceEmergencyHiddenInput(serviceIndex);
    const serviceDetails = serviceSelect ? servicesCatalog[serviceSelect.value] : null;
    const supportsEmergency = serviceSupportsEmergency(serviceDetails);

    if (!emergencyCheckbox) {
      return;
    }

    if (!serviceSelect || !serviceSelect.value) {
      emergencyCheckbox.checked = false;
      if (hiddenInput) {
        hiddenInput.value = 'no';
      }
      setEmergencyWarningVisible(serviceIndex, false);
      return;
    }

    if (!supportsEmergency) {
      const wasChecked = emergencyCheckbox.checked;
      emergencyCheckbox.checked = false;
      if (hiddenInput) {
        hiddenInput.value = 'no';
      }
      setEmergencyWarningVisible(serviceIndex, !!(showWarningWhenUnsupported && wasChecked));
      return;
    }

    if (hiddenInput) {
      hiddenInput.value = emergencyCheckbox.checked ? 'yes' : 'no';
    }
    setEmergencyWarningVisible(serviceIndex, false);
  }

  function setServiceRowPriceFromCatalog(serviceIndex) {
    const serviceSelect = document.getElementById('service-code-' + serviceIndex);
    const priceInput = getServicePriceInput(serviceIndex);
    const serviceDetails = serviceSelect ? servicesCatalog[serviceSelect.value] : null;
    const emergencyCheckbox = getServiceEmergencyCheckbox(serviceIndex);
    const useEmergencyPrice = !!(emergencyCheckbox && emergencyCheckbox.checked && serviceSupportsEmergency(serviceDetails));

    if (!priceInput) {
      return;
    }

    if (!serviceSelect || !serviceSelect.value) {
      priceInput.value = '$0.00';
      return;
    }

    if (!serviceDetails) {
      return;
    }

    if (useEmergencyPrice) {
      priceInput.value = serviceDetails.emergency_price || serviceDetails.standard_price || serviceDetails.price || '$0.00';
      return;
    }

    priceInput.value = serviceDetails.standard_price || serviceDetails.price || '$0.00';
  }

  function attachEmergencyToggleListener(checkboxElement) {
    if (!checkboxElement) {
      return;
    }

    checkboxElement.addEventListener('change', function () {
      const serviceIndex = this.dataset.serviceIndex || this.id.split('-')[3];
      const hiddenInput = getServiceEmergencyHiddenInput(serviceIndex);

      syncEmergencyAvailability(serviceIndex, true);

      if (hiddenInput) {
        hiddenInput.value = this.checked ? 'yes' : 'no';
      }

      setServiceRowPriceFromCatalog(serviceIndex);
      validateForm();
    });

    const initialServiceIndex = checkboxElement.dataset.serviceIndex || checkboxElement.id.split('-')[3];
    syncEmergencyAvailability(initialServiceIndex, false);
  }

  function attachPriceUpdateListener(selectElement) {
    selectElement.addEventListener('change', function () {
      servicesTouched = true; // Mark services as touched
      const selectedService = this.value;
      const serviceIndex = this.id.split('-')[2]; // Extract index from id like 'service-code-1'
      const priceInput = getServicePriceInput(serviceIndex);
      const durationInput = getServiceHoursInput(serviceIndex);
      const serviceDetails = selectedService ? servicesCatalog[selectedService] : null;
      
      if (priceInput) {
        if (serviceDetails && (serviceDetails.standard_price || serviceDetails.price || serviceDetails.emergency_price)) {
          syncEmergencyAvailability(serviceIndex, true);
          setServiceRowPriceFromCatalog(serviceIndex);
        } else if (!selectedService) {
          syncEmergencyAvailability(serviceIndex, false);
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
      autoPopulateMaterialsForService(selectedService);

      validateForm();
    });
  }
  const initialSelects = document.querySelectorAll('select[id^="service-code-"]');
  initialSelects.forEach(function (select) {
    attachPriceUpdateListener(select);
  });

  const initialEmergencyToggles = document.querySelectorAll('.service-emergency-call-toggle');
  initialEmergencyToggles.forEach(function (checkbox) {
    attachEmergencyToggleListener(checkbox);
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
  const addEquipmentButton = document.getElementById("add-job-equipment-button");
  const equipmentsList = document.getElementById("job-equipments-list");
  const equipmentOptionsTemplate = document.getElementById("equipment-options-template");
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

  function getEquipmentSelect(rowElement) {
    return rowElement ? rowElement.querySelector('select[name="equipment_name[]"]') : null;
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

  function isEquipmentRowPopulated(rowElement) {
    const select = getEquipmentSelect(rowElement);
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
            : rowType === 'equipment'
              ? (equipmentsList ? equipmentsList.querySelectorAll('.job-equipment-row') : [])
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
            : rowType === 'equipment'
              ? isEquipmentRowPopulated(rowElement)
            : isDiscountRowPopulated(rowElement);

    removeWrap.style.display = (isPopulated || hasMultipleRows) ? '' : 'none';
  }

  function refreshRemoveButtons(rowType) {
    const listElement = rowType === 'service' ? servicesList : rowType === 'part' ? partsList : rowType === 'labor' ? laborsList : rowType === 'material' ? materialsList : rowType === 'equipment' ? equipmentsList : discountsList;
    const selector = rowType === 'service' ? '.job-service-row' : rowType === 'part' ? '.job-part-row' : rowType === 'labor' ? '.job-labor-row' : rowType === 'material' ? '.job-material-row' : rowType === 'equipment' ? '.job-equipment-row' : '.job-discount-row';

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
        '<label for="service-price-' + nextIndex + '">Price</label>' +
        '<input id="service-price-' + nextIndex + '" name="service_price[]" type="text" placeholder="$0.00" />' +
      '</div>' +
      '<div class="add-customer-form-field">' +
        '<label for="service-hours-' + nextIndex + '">Hours</label>' +
        '<input id="service-hours-' + nextIndex + '" name="service_hours[]" type="number" step="0.25" min="0" placeholder="0" />' +
      '</div>' +
      '<div class="add-customer-form-field service-emergency-call-field">' +
        '<label for="service-emergency-call-' + nextIndex + '">Emergency Call?</label>' +
        '<input id="service-emergency-call-hidden-' + nextIndex + '" name="service_emergency_call[]" type="hidden" value="no" />' +
        '<input id="service-emergency-call-' + nextIndex + '" class="service-emergency-call-toggle" data-service-index="' + nextIndex + '" type="checkbox" />' +
      '</div>';

    const newSelect = row.querySelector('select');
    if (newSelect) {
      attachPriceUpdateListener(newSelect);
    }

    const newEmergencyToggle = row.querySelector('.service-emergency-call-toggle');
    if (newEmergencyToggle) {
      attachEmergencyToggleListener(newEmergencyToggle);
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

  function createEquipmentRow() {
    const nextIndex = getNextRowIndex(equipmentsList, '.job-equipment-row', 'equipmentIndex');
    const row = document.createElement("div");
    row.className = "add-customer-form-row job-equipment-row";
    row.dataset.equipmentIndex = String(nextIndex);

    const optionHtml = equipmentOptionsTemplate.innerHTML;

    row.innerHTML =
      '<div class="add-customer-form-field">' +
        '<label for="equipment-name-' + nextIndex + '">Equipment</label>' +
        '<select id="equipment-name-' + nextIndex + '" name="equipment_name[]">' +
          '<option value="">-- Select equipment --</option>' +
          optionHtml +
        '</select>' +
      '</div>' +
      '<div class="add-customer-form-field">' +
        '<label for="equipment-quantity-installed-' + nextIndex + '">Quantity Installed</label>' +
        '<input id="equipment-quantity-installed-' + nextIndex + '" name="equipment_quantity_installed[]" type="number" step="0.25" min="0" placeholder="0" />' +
      '</div>' +
      '<div class="add-customer-form-field">' +
        '<label for="equipment-price-' + nextIndex + '">Price</label>' +
        '<input id="equipment-price-' + nextIndex + '" name="equipment_price[]" type="text" placeholder="$0.00" />' +
      '</div>';

    const newSelect = row.querySelector('select');
    if (newSelect) {
      attachEquipmentDefaultsListener(newSelect);
    }

    attachRowRemoveButton(row, 'equipment');
    updateRemoveButtonVisibility(row, 'equipment');
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

  function ensureAtLeastOneEquipmentRow() {
    if (!equipmentsList || !equipmentOptionsTemplate) {
      return;
    }

    const rows = equipmentsList.querySelectorAll('.job-equipment-row');
    if (rows.length === 0) {
      equipmentsList.appendChild(createEquipmentRow());
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
    removeButton.textContent = rowType === 'service' ? 'Remove Service' : rowType === 'part' ? 'Remove Part' : rowType === 'labor' ? 'Remove Labor' : rowType === 'material' ? 'Remove Material' : rowType === 'equipment' ? 'Remove Equipment' : 'Remove Discount';

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
      } else if (rowType === 'equipment') {
        ensureAtLeastOneEquipmentRow();
        refreshRemoveButtons('equipment');
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
    const servicePartEntries = serviceDetails && serviceDetails.service_parts ? serviceDetails.service_parts : [];
    const servicePartEntryById = {};
    servicePartEntries.forEach(function (entry) {
      if (entry && entry.part_id) {
        servicePartEntryById[entry.part_id] = entry;
      }
    });

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
        const servicePartEntry = servicePartEntryById[partId];
        if (costInput) {
          if (servicePartEntry && servicePartEntry.unit_cost) {
            costInput.value = servicePartEntry.unit_cost;
          } else if (partDetails) {
            costInput.value = partDetails.unit_cost || partDetails.price || '';
          }
        }
        updateRemoveButtonVisibility(targetRow, 'part');
      }
    });

    refreshRemoveButtons('part');
  }

  function autoPopulateMaterialsForService(serviceCode) {
    if (!materialsList || !materialOptionsTemplate) return;

    materialsList.querySelectorAll('.job-material-row[data-auto-populated]').forEach(function (row) {
      row.remove();
    });

    const serviceDetails = serviceCode ? servicesCatalog[serviceCode] : null;
    const materialIds = serviceDetails && serviceDetails.material_ids ? serviceDetails.material_ids : [];
    const serviceMaterialEntries = serviceDetails && serviceDetails.service_materials ? serviceDetails.service_materials : [];
    const serviceMaterialEntryById = {};
    serviceMaterialEntries.forEach(function (entry) {
      if (entry && entry.material_id) {
        serviceMaterialEntryById[entry.material_id] = entry;
      }
    });

    if (!materialIds.length) {
      ensureAtLeastOneMaterialRow();
      refreshRemoveButtons('material');
      return;
    }

    const existingNames = new Set();
    materialsList.querySelectorAll('.job-material-row').forEach(function (row) {
      const sel = getMaterialSelect(row);
      if (sel && sel.value.trim()) existingNames.add(sel.value.trim());
    });

    const currentRows = materialsList.querySelectorAll('.job-material-row');
    const isOnlyEmptyRow = currentRows.length === 1 && !isMaterialRowPopulated(currentRows[0]);
    let reusedEmptyRow = false;

    materialIds.forEach(function (materialId) {
      const materialName = materialsById[materialId];
      if (!materialName || existingNames.has(materialName)) return;
      existingNames.add(materialName);

      let targetRow;
      if (isOnlyEmptyRow && !reusedEmptyRow) {
        targetRow = currentRows[0];
        reusedEmptyRow = true;
      } else {
        targetRow = createMaterialRow();
        materialsList.appendChild(targetRow);
      }

      targetRow.dataset.autoPopulated = 'true';

      const sel = getMaterialSelect(targetRow);
      if (sel) {
        sel.value = materialName;
        const idx = sel.id.replace('material-name-', '');
        const quantityInput = document.getElementById('material-quantity-used-' + idx);
        const unitInput = document.getElementById('material-unit-of-measure-' + idx);
        const priceInput = document.getElementById('material-price-' + idx);
        const details = materialsCatalog[materialName];
        const serviceMaterialEntry = serviceMaterialEntryById[materialId];

        if (quantityInput) {
          if (serviceMaterialEntry && serviceMaterialEntry.default_quantity_used) {
            quantityInput.value = serviceMaterialEntry.default_quantity_used;
          } else if (details && details.default_quantity_used) {
            quantityInput.value = details.default_quantity_used;
          }
        }
        if (unitInput) {
          if (serviceMaterialEntry && serviceMaterialEntry.unit_of_measure) {
            unitInput.value = serviceMaterialEntry.unit_of_measure;
          } else if (details && details.unit_of_measure) {
            unitInput.value = details.unit_of_measure;
          }
        }
        if (priceInput) {
          if (serviceMaterialEntry && serviceMaterialEntry.price) {
            priceInput.value = serviceMaterialEntry.price;
          } else if (details && details.price) {
            priceInput.value = details.price;
          }
        }

        updateRemoveButtonVisibility(targetRow, 'material');
      }
    });

    refreshRemoveButtons('material');
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

  function attachEquipmentDefaultsListener(selectElement) {
    selectElement.addEventListener('change', function () {
      const selectedEquipment = this.value;
      const equipmentIndex = this.id.split('-')[2];
      const quantityInput = document.getElementById('equipment-quantity-installed-' + equipmentIndex);
      const priceInput = document.getElementById('equipment-price-' + equipmentIndex);
      const equipmentDetails = selectedEquipment ? equipmentsCatalog[selectedEquipment] : null;

      if (quantityInput) {
        if (equipmentDetails && equipmentDetails.default_quantity_installed) {
          quantityInput.value = equipmentDetails.default_quantity_installed;
        } else if (!selectedEquipment) {
          quantityInput.value = '';
        }
      }

      if (priceInput) {
        if (equipmentDetails && equipmentDetails.default_price) {
          priceInput.value = equipmentDetails.default_price;
        } else if (!selectedEquipment) {
          priceInput.value = '$0.00';
        }
      }

      const equipmentRow = this.closest('.job-equipment-row');
      if (equipmentRow) {
        updateRemoveButtonVisibility(equipmentRow, 'equipment');
      }

      refreshRemoveButtons('equipment');
    });
  }

  const initialEquipmentSelects = document.querySelectorAll('select[id^="equipment-name-"]');
  initialEquipmentSelects.forEach(function (select) {
    attachEquipmentDefaultsListener(select);
    if (select.value) {
      select.dispatchEvent(new Event('change'));
    }
  });

  const initialEquipmentRows = equipmentsList ? equipmentsList.querySelectorAll('.job-equipment-row') : [];
  initialEquipmentRows.forEach(function (row) {
    attachRowRemoveButton(row, 'equipment');
  });

  ensureAtLeastOneEquipmentRow();
  refreshRemoveButtons('equipment');

  if (addEquipmentButton && equipmentsList && equipmentOptionsTemplate) {
    addEquipmentButton.addEventListener("click", function () {
      equipmentsList.appendChild(createEquipmentRow());
      refreshRemoveButtons('equipment');

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

  [scheduleTypeField, recurringFrequencyField, recurringEndTypeField, recurringEndDateField, recurringEndAfterField].forEach(function (field) {
    if (!field) {
      return;
    }

    field.addEventListener('change', function () {
      if (field === scheduleTypeField) {
        syncRecurringSettingsVisibility();
      }
      if (field === recurringEndTypeField) {
        syncRecurringEndFields();
      }
      validateForm();
    });

    field.addEventListener('input', function () {
      validateForm();
    });
  });

  if (clearDateTimeButton) {
    clearDateTimeButton.addEventListener('click', function () {
      if (clearableDateField) {
        clearableDateField.value = '';
      }
      if (clearableTimeField) {
        clearableTimeField.value = '';
      }
      dateTouched = false;
      timeTouched = false;
      validateForm();
    });
  }

  // Validate on form submission
  form.addEventListener('submit', function (event) {
    submitAttempted = true;
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
  syncRecurringSettingsVisibility();
   validateForm();
})();
