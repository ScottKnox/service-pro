(function () {
  const filterRoots = document.querySelectorAll('[data-catalog-filter-root]');

  function normalizeValue(value) {
    return String(value || '').trim().toLowerCase();
  }

  filterRoots.forEach(function (root) {
    const cards = Array.from(root.querySelectorAll('[data-catalog-card]'));
    const noResultsMessage = root.querySelector('#catalog-filter-no-results');

    function getCheckedValues(filterName) {
      return new Set(
        Array.from(root.querySelectorAll('input[name="' + filterName + '"]:checked')).map(function (input) {
          return normalizeValue(input.value);
        })
      );
    }

    function applyFilters() {
      const selectedCategories = getCheckedValues('filter_category');
      const selectedCodes = getCheckedValues('filter_code');
      let visibleCount = 0;

      cards.forEach(function (card) {
        const category = normalizeValue(card.dataset.filterCategory);
        const code = normalizeValue(card.dataset.filterCode);
        const matchesCategory = selectedCategories.size === 0 || selectedCategories.has(category);
        const matchesCode = selectedCodes.size === 0 || selectedCodes.has(code);
        const shouldShow = matchesCategory && matchesCode;
        card.style.display = shouldShow ? '' : 'none';
        if (shouldShow) {
          visibleCount += 1;
        }
      });

      if (noResultsMessage) {
        noResultsMessage.hidden = visibleCount !== 0;
      }
    }

    root.querySelectorAll('input[type="checkbox"]').forEach(function (checkbox) {
      checkbox.addEventListener('change', applyFilters);
    });

    applyFilters();
  });
})();