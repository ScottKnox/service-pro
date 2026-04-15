document.addEventListener('DOMContentLoaded', () => {
  const roots = document.querySelectorAll('[data-job-overview-pagination]');

  roots.forEach((root) => {
    const rows = Array.from(root.querySelectorAll('[data-page-item]'));
    const controls = root.querySelector('[data-pagination-controls]');
    const summary = root.querySelector('[data-pagination-summary]');
    const links = root.querySelector('[data-pagination-links]');
    const pageSize = Math.max(parseInt(root.getAttribute('data-page-size') || '5', 10), 1);
    const pageCount = Math.ceil(rows.length / pageSize);

    if (!controls || !summary || !links || pageCount <= 1) {
      return;
    }

    let currentPage = 1;

    const renderPage = (pageNumber) => {
      currentPage = pageNumber;
      const startIndex = (currentPage - 1) * pageSize;
      const endIndex = startIndex + pageSize;

      rows.forEach((row, index) => {
        row.hidden = index < startIndex || index >= endIndex;
      });

      const visibleEnd = Math.min(endIndex, rows.length);
      summary.textContent = `Showing ${startIndex + 1}-${visibleEnd} of ${rows.length} employees`;

      Array.from(links.querySelectorAll('.reporting-jobs-page-link')).forEach((button, index) => {
        const isActive = index + 1 === currentPage;
        button.classList.toggle('is-active', isActive);
        button.setAttribute('aria-current', isActive ? 'page' : 'false');
      });
    };

    links.innerHTML = '';
    for (let pageIndex = 1; pageIndex <= pageCount; pageIndex += 1) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'reporting-jobs-page-link';
      button.textContent = String(pageIndex);
      button.setAttribute('aria-label', `Go to employee page ${pageIndex}`);
      button.addEventListener('click', () => renderPage(pageIndex));
      links.appendChild(button);
    }

    controls.hidden = false;
    renderPage(1);
  });
});
