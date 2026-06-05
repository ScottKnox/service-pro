(function () {
  function fieldContainerById(fieldId) {
    const element = document.getElementById(fieldId);
    return element ? element.closest('.add-customer-form-field') : null;
  }

  const specFieldIds = [
    'equipment-cooling-capacity',
    'equipment-seer-rating',
    'equipment-metering-device',
    'equipment-afue-rating',
    'equipment-btu-input',
    'equipment-btu-output',
    'equipment-refrigerant-type',
    'equipment-stages',
    'equipment-blower-motor-type',
    'equipment-voltage',
  ];

  const fieldsByType = {
    'AC Condenser': [
      'equipment-cooling-capacity',
      'equipment-seer-rating',
      'equipment-refrigerant-type',
      'equipment-stages',
      'equipment-voltage',
    ],
    'Gas Furnace': [
      'equipment-afue-rating',
      'equipment-btu-input',
      'equipment-btu-output',
      'equipment-stages',
      'equipment-blower-motor-type',
      'equipment-voltage',
    ],
    'Heat Pump Condenser': [
      'equipment-cooling-capacity',
      'equipment-seer-rating',
      'equipment-refrigerant-type',
      'equipment-stages',
      'equipment-voltage',
    ],
    'Air Handler': [
      'equipment-btu-input',
      'equipment-btu-output',
      'equipment-blower-motor-type',
      'equipment-voltage',
    ],
    'Mini Split Outdoor Unit': [
      'equipment-cooling-capacity',
      'equipment-seer-rating',
      'equipment-refrigerant-type',
      'equipment-stages',
      'equipment-voltage',
    ],
    'Mini Split Indoor Unit': [
      'equipment-btu-input',
      'equipment-btu-output',
      'equipment-voltage',
    ],
    'Package Unit': [
      'equipment-cooling-capacity',
      'equipment-seer-rating',
      'equipment-afue-rating',
      'equipment-btu-input',
      'equipment-btu-output',
      'equipment-refrigerant-type',
      'equipment-stages',
      'equipment-blower-motor-type',
      'equipment-voltage',
    ],
    'Other': specFieldIds.slice(),
  };

  function setSpecVisibility() {
    const equipmentTypeSelect = document.getElementById('equipment-type');
    const emptyState = document.getElementById('equipment-spec-empty-state');
    if (!equipmentTypeSelect) {
      return;
    }

    const selectedType = String(equipmentTypeSelect.value || '').trim();
    const visibleFieldIds = selectedType && fieldsByType[selectedType] ? fieldsByType[selectedType] : [];

    specFieldIds.forEach(function (fieldId) {
      const container = fieldContainerById(fieldId);
      if (!container) {
        return;
      }
      container.style.display = visibleFieldIds.indexOf(fieldId) >= 0 ? '' : 'none';
    });

    const specRows = document.querySelectorAll('.equipment-spec-row');
    specRows.forEach(function (row) {
      const rowFields = row.querySelectorAll('.add-customer-form-field');
      let hasVisibleField = false;

      rowFields.forEach(function (field) {
        if (window.getComputedStyle(field).display !== 'none') {
          hasVisibleField = true;
        }
      });

      row.style.display = hasVisibleField ? '' : 'none';
    });

    if (emptyState) {
      emptyState.style.display = visibleFieldIds.length > 0 ? 'none' : '';
    }
  }

  const equipmentTypeSelect = document.getElementById('equipment-type');
  if (!equipmentTypeSelect) {
    return;
  }

  equipmentTypeSelect.addEventListener('change', setSpecVisibility);
  setSpecVisibility();
})();
