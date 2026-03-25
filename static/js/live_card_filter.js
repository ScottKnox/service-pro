(function () {
  const filterRoots = document.querySelectorAll('[data-live-card-filter-root]');

  filterRoots.forEach(function (root) {
    const input = root.querySelector('[data-live-card-filter-input]');
    const form = root.querySelector('[data-live-card-filter-form]');
    const grid = root.querySelector('[data-live-card-filter-grid]');
    const emptyMessage = root.querySelector('[data-live-card-filter-empty]');

    if (!input || !grid) {
      return;
    }

    const cards = Array.from(grid.querySelectorAll('[data-live-card-filter-item]'));

    function applyFilter() {
      const query = input.value.trim().toLowerCase();
      let visibleCount = 0;

      cards.forEach(function (card) {
        const firstNameElement = card.querySelector('[data-live-card-first-name]');
        const lastNameElement = card.querySelector('[data-live-card-last-name]');
        const firstName = firstNameElement ? firstNameElement.textContent.trim().toLowerCase() : '';
        const lastName = lastNameElement ? lastNameElement.textContent.trim().toLowerCase() : '';
        const isMatch = query === '' || firstName.indexOf(query) !== -1 || lastName.indexOf(query) !== -1;

        card.style.display = isMatch ? '' : 'none';
        if (isMatch) {
          visibleCount += 1;
        }
      });

      if (emptyMessage) {
        emptyMessage.hidden = visibleCount !== 0 || cards.length === 0;
      }
    }

    if (form) {
      form.addEventListener('submit', function (event) {
        event.preventDefault();
        applyFilter();
      });
    }

    input.addEventListener('input', applyFilter);
    input.addEventListener('search', applyFilter);

    applyFilter();
  });
})();