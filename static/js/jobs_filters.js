(function () {
  const jobsGrid = document.getElementById('jobs-list-grid');
  if (!jobsGrid) {
    return;
  }

  const jobCards = Array.from(jobsGrid.querySelectorAll('.jobs-card'));
  if (jobCards.length === 0) {
    return;
  }

  const employeeOptions = document.getElementById('jobs-filter-employee-options');
  const cityOptions = document.getElementById('jobs-filter-city-options');
  const stateOptions = document.getElementById('jobs-filter-state-options');
  const startDateInput = document.getElementById('jobs-filter-start-date');
  const endDateInput = document.getElementById('jobs-filter-end-date');
  const clearDateFiltersButton = document.getElementById('jobs-clear-date-filters');
  const noResultsMessage = document.getElementById('jobs-filter-no-results');

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

    jobCards.forEach(function (card) {
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

  function applyFilters() {
    const selectedEmployees = getCheckedValues('filter_employee');
    const selectedCities = getCheckedValues('filter_city');
    const selectedStates = getCheckedValues('filter_state');
    const startDate = startDateInput ? parseIsoDate(startDateInput.value) : null;
    const endDate = endDateInput ? parseIsoDate(endDateInput.value) : null;

    let visibleCount = 0;

    jobCards.forEach(function (card) {
      const employee = normalizeValue(card.dataset.filterEmployee);
      const city = normalizeValue(card.dataset.filterCity);
      const state = normalizeValue(card.dataset.filterState);
      const scheduledDate = parseUsDate(card.dataset.filterDate);

      const matchesEmployee = selectedEmployees.size === 0 || selectedEmployees.has(employee);
      const matchesCity = selectedCities.size === 0 || selectedCities.has(city);
      const matchesState = selectedStates.size === 0 || selectedStates.has(state);

      let matchesDate = true;
      if (startDate && (!scheduledDate || scheduledDate < startDate)) {
        matchesDate = false;
      }
      if (matchesDate && endDate && (!scheduledDate || scheduledDate > endDate)) {
        matchesDate = false;
      }

      const shouldShow = matchesEmployee && matchesCity && matchesState && matchesDate;
      card.style.display = shouldShow ? '' : 'none';
      if (shouldShow) {
        visibleCount += 1;
      }
    });

    if (noResultsMessage) {
      noResultsMessage.hidden = visibleCount !== 0;
    }
  }

  renderCheckboxOptions(employeeOptions, 'filter_employee', getUniqueOptions('filterEmployee'), 'No employees available.');
  renderCheckboxOptions(cityOptions, 'filter_city', getUniqueOptions('filterCity'), 'No cities available.');
  renderCheckboxOptions(stateOptions, 'filter_state', getUniqueOptions('filterState'), 'No states available.');

  [employeeOptions, cityOptions, stateOptions].forEach(function (container) {
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
