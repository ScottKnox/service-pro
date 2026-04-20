(function () {
  const estimatesGrid = document.getElementById('estimates-list-grid');
  if (!estimatesGrid) {
    return;
  }

  const estimateCards = Array.from(estimatesGrid.querySelectorAll('.jobs-card'));
  if (estimateCards.length === 0) {
    return;
  }

  const employeeOptions = document.getElementById('estimates-filter-employee-options');
  const statusOptions = document.getElementById('estimates-filter-status-options');
  const cityOptions = document.getElementById('estimates-filter-city-options');
  const stateOptions = document.getElementById('estimates-filter-state-options');
  const startDateInput = document.getElementById('estimates-filter-start-date');
  const endDateInput = document.getElementById('estimates-filter-end-date');
  const clearDateFiltersButton = document.getElementById('estimates-clear-date-filters');
  const noResultsMessage = document.getElementById('estimates-filter-no-results');

  function normalizeValue(value) {
    return String(value || '').trim().toLowerCase();
  }

  function parseUsDate(value) {
    const dateText = String(value || '').trim();
    const match = /^(\d{1,2})\/(\d{1,2})\/(\d{4})$/.exec(dateText);
    if (!match) {
      return null;
    }

    const month = Number.parseInt(match[1], 10) - 1;
    const day = Number.parseInt(match[2], 10);
    const year = Number.parseInt(match[3], 10);
    const parsed = new Date(year, month, day);
    parsed.setHours(0, 0, 0, 0);
    return parsed;
  }

  function parseIsoDate(value) {
    const dateText = String(value || '').trim();
    if (!dateText) {
      return null;
    }

    const parsed = new Date(dateText + 'T00:00:00');
    if (Number.isNaN(parsed.getTime())) {
      return null;
    }

    parsed.setHours(0, 0, 0, 0);
    return parsed;
  }

  function getUniqueOptions(attributeName) {
    const uniqueMap = new Map();

    estimateCards.forEach(function (card) {
      const label = String(card.dataset[attributeName] || '').trim();
      const normalized = normalizeValue(label);
      if (!normalized || uniqueMap.has(normalized)) {
        return;
      }

      uniqueMap.set(normalized, label);
    });

    return Array.from(uniqueMap.entries())
      .map(function (entry) {
        return { value: entry[0], label: entry[1] };
      })
      .sort(function (a, b) {
        return a.label.localeCompare(b.label, undefined, { sensitivity: 'base' });
      });
  }

  function renderCheckboxOptions(container, filterName, options, emptyLabel) {
    if (!container) {
      return;
    }

    if (options.length === 0) {
      container.innerHTML = '<p>' + emptyLabel + '</p>';
      return;
    }

    container.innerHTML = options
      .map(function (option, index) {
        const inputId = filterName + '-' + index;
        return (
          '<label for="' + inputId + '">' +
            '<input id="' + inputId + '" type="checkbox" name="' + filterName + '" value="' + option.value + '" /> ' +
            option.label +
          '</label>'
        );
      })
      .join('');
  }

  function getCheckedValues(filterName) {
    return new Set(
      Array.from(document.querySelectorAll('input[name="' + filterName + '"]:checked')).map(function (input) {
        return normalizeValue(input.value);
      })
    );
  }

  function parseQueryValues(paramNames) {
    const searchParams = new URLSearchParams(window.location.search);
    const values = new Set();

    paramNames.forEach(function (paramName) {
      const raw = searchParams.get(paramName);
      if (!raw) {
        return;
      }

      raw
        .split(',')
        .map(function (value) {
          return normalizeValue(value);
        })
        .filter(Boolean)
        .forEach(function (value) {
          values.add(value);
        });
    });

    return values;
  }

  function applyCheckedValues(filterName, values) {
    if (!values || values.size === 0) {
      return;
    }

    const inputs = document.querySelectorAll('input[name="' + filterName + '"]');
    inputs.forEach(function (input) {
      input.checked = values.has(normalizeValue(input.value));
    });
  }

  function applyFilters() {
    const selectedEmployees = getCheckedValues('filter_estimate_employee');
    const selectedStatuses = getCheckedValues('filter_estimate_status');
    const selectedCities = getCheckedValues('filter_estimate_city');
    const selectedStates = getCheckedValues('filter_estimate_state');
    const startDate = startDateInput ? parseIsoDate(startDateInput.value) : null;
    const endDate = endDateInput ? parseIsoDate(endDateInput.value) : null;

    let visibleCount = 0;

    estimateCards.forEach(function (card) {
      const employee = normalizeValue(card.dataset.filterEmployee);
      const status = normalizeValue(card.dataset.filterStatus);
      const city = normalizeValue(card.dataset.filterCity);
      const state = normalizeValue(card.dataset.filterState);
      const createdDate = parseUsDate(card.dataset.filterDate);

      const matchesEmployee = selectedEmployees.size === 0 || selectedEmployees.has(employee);
      const matchesStatus = selectedStatuses.size === 0 || selectedStatuses.has(status);
      const matchesCity = selectedCities.size === 0 || selectedCities.has(city);
      const matchesState = selectedStates.size === 0 || selectedStates.has(state);

      let matchesDate = true;
      if (startDate && (!createdDate || createdDate < startDate)) {
        matchesDate = false;
      }
      if (matchesDate && endDate && (!createdDate || createdDate > endDate)) {
        matchesDate = false;
      }

      const shouldShow = matchesEmployee && matchesStatus && matchesCity && matchesState && matchesDate;
      card.style.display = shouldShow ? '' : 'none';
      if (shouldShow) {
        visibleCount += 1;
      }
    });

    if (noResultsMessage) {
      noResultsMessage.hidden = visibleCount !== 0;
    }
  }

  renderCheckboxOptions(employeeOptions, 'filter_estimate_employee', getUniqueOptions('filterEmployee'), 'No employees available.');
  renderCheckboxOptions(statusOptions, 'filter_estimate_status', getUniqueOptions('filterStatus'), 'No statuses available.');
  renderCheckboxOptions(cityOptions, 'filter_estimate_city', getUniqueOptions('filterCity'), 'No cities available.');
  renderCheckboxOptions(stateOptions, 'filter_estimate_state', getUniqueOptions('filterState'), 'No states available.');

  applyCheckedValues('filter_estimate_status', parseQueryValues(['status', 'filter_status']));

  [employeeOptions, statusOptions, cityOptions, stateOptions].forEach(function (container) {
    if (!container) {
      return;
    }
    container.addEventListener('change', applyFilters);
  });

  if (startDateInput) {
    startDateInput.addEventListener('input', applyFilters);
    startDateInput.addEventListener('change', applyFilters);
  }

  if (endDateInput) {
    endDateInput.addEventListener('input', applyFilters);
    endDateInput.addEventListener('change', applyFilters);
  }

  if (clearDateFiltersButton) {
    clearDateFiltersButton.addEventListener('click', function () {
      if (startDateInput) {
        startDateInput.value = '';
      }
      if (endDateInput) {
        endDateInput.value = '';
      }
      applyFilters();
    });
  }

  applyFilters();
})();
