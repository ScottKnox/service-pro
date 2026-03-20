// Shared filter state for home page
window.homePageFilter = {
  filterData: {
    selectedDate: null,
    selectedEmployees: new Set(),
  },
  currentPage: 1,
  JOBS_PER_PAGE: 5,
  
  filterAndDisplay: function() {
    const jobElements = document.querySelectorAll(".home-job");
    const pagesContainer = document.getElementById("home-jobs-pages");
    const visibleJobs = Array.from(jobElements).filter((job) => {
      const jobDate = job.dataset.jobDate;
      const jobEmployee = job.dataset.assignedEmployee;

      // Filter by date if a date is selected
      if (this.filterData.selectedDate && jobDate !== this.filterData.selectedDate) {
        return false;
      }

      // Filter by employee if any employees are selected
      if (this.filterData.selectedEmployees.size > 0 && !this.filterData.selectedEmployees.has(jobEmployee)) {
        return false;
      }

      return true;
    });

    // Reset to page 1 and update pagination
    this.currentPage = 1;
    this.updatePaginationLinks(visibleJobs.length);
    this.displayPageContent();
  },

  updatePaginationLinks: function(visibleJobsCount) {
    const pagesContainer = document.getElementById("home-jobs-pages");
    const totalPages = Math.ceil(visibleJobsCount / this.JOBS_PER_PAGE) || 1;
    const existingLinks = pagesContainer.querySelectorAll("a");
    
    // Remove old page links
    existingLinks.forEach((link) => link.remove());

    // Create new page links
    for (let i = 1; i <= totalPages; i++) {
      const link = document.createElement("a");
      link.href = "#";
      link.textContent = String(i);
      if (i === 1) {
        link.classList.add("is-active");
      }
      link.addEventListener("click", (event) => {
        event.preventDefault();
        this.currentPage = i;
        this.displayPageContent();
      });
      pagesContainer.appendChild(link);
    }
  },

  displayPageContent: function() {
    const jobElements = document.querySelectorAll(".home-job");
    const pagesContainer = document.getElementById("home-jobs-pages");
    const visibleJobs = Array.from(jobElements).filter((job) => {
      const jobDate = job.dataset.jobDate;
      const jobEmployee = job.dataset.assignedEmployee;

      // Filter by date if a date is selected
      if (this.filterData.selectedDate && jobDate !== this.filterData.selectedDate) {
        return false;
      }

      // Filter by employee if any employees are selected
      if (this.filterData.selectedEmployees.size > 0 && !this.filterData.selectedEmployees.has(jobEmployee)) {
        return false;
      }

      return true;
    });

    const startIndex = (this.currentPage - 1) * this.JOBS_PER_PAGE;
    const endIndex = startIndex + this.JOBS_PER_PAGE;

    // Show/hide jobs based on current page
    jobElements.forEach((job) => {
      job.style.display = "none";
    });

    visibleJobs.slice(startIndex, endIndex).forEach((job) => {
      job.style.display = "";
    });

    // Update active page link
    const pageLinks = pagesContainer.querySelectorAll("a");
    pageLinks.forEach((link) => link.classList.remove("is-active"));
    if (pageLinks[this.currentPage - 1]) {
      pageLinks[this.currentPage - 1].classList.add("is-active");
    }

    // Show/hide empty state message
    const emptyMessage = document.getElementById("home-jobs-empty");
    if (emptyMessage) {
      emptyMessage.style.display = visibleJobs.length === 0 ? "block" : "none";
    }
  }
};

// Jobs filter and pagination IIFE
(() => {
  function getSelectedEmployees() {
    const checkboxes = document.querySelectorAll("input[name='home-jobs-assignee']:checked");
    const selected = new Set();
    checkboxes.forEach((checkbox) => {
      selected.add(checkbox.value);
    });
    return selected;
  }

  // Handle employee checkbox changes
  const employeeCheckboxes = document.querySelectorAll("input[name='home-jobs-assignee']");
  employeeCheckboxes.forEach((checkbox) => {
    checkbox.addEventListener("change", () => {
      // Ensure at least one employee is selected
      const checkedBoxes = document.querySelectorAll("input[name='home-jobs-assignee']:checked");
      if (checkedBoxes.length === 0) {
        // Prevent unchecking - revert the change
        checkbox.checked = true;
        return;
      }
      window.homePageFilter.filterData.selectedEmployees = getSelectedEmployees();
      window.homePageFilter.filterAndDisplay();
    });
  });

  // Initialize with checked employees
  window.homePageFilter.filterData.selectedEmployees = getSelectedEmployees();
  window.homePageFilter.filterAndDisplay();
})();

// Calendar IIFE
(() => {
  const monthLabel = document.getElementById("home-calendar-month-label");
  const daysContainer = document.getElementById("home-calendar-days");
  const prevButton = document.getElementById("home-calendar-prev");
  const nextButton = document.getElementById("home-calendar-next");

  if (!monthLabel || !daysContainer || !prevButton || !nextButton) {
    return;
  }

  const today = new Date();
  const selectedDays = new Set([
    dayKey(today.getFullYear(), today.getMonth(), today.getDate()),
  ]);
  const visibleMonth = new Date();
  visibleMonth.setDate(1);

  function dayKey(year, month, day) {
    return `${year}-${String(month + 1).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
  }

  function todayKey() {
    return dayKey(today.getFullYear(), today.getMonth(), today.getDate());
  }

  function yesterdayKey() {
    const d = new Date(today);
    d.setDate(today.getDate() - 1);
    return dayKey(d.getFullYear(), d.getMonth(), d.getDate());
  }

  function tomorrowKey() {
    const d = new Date(today);
    d.setDate(today.getDate() + 1);
    return dayKey(d.getFullYear(), d.getMonth(), d.getDate());
  }

  function updateHeadline(key) {
    const headline = document.querySelector(".home-jobs-column h2");
    if (!headline) return;
    if (!key || key === todayKey()) {
      headline.textContent = "Today's Jobs";
    } else if (key === yesterdayKey()) {
      headline.textContent = "Yesterday's Jobs";
    } else if (key === tomorrowKey()) {
      headline.textContent = "Tomorrow's Jobs";
    } else {
      const [y, m, d] = key.split("-").map(Number);
      const formatted = new Date(y, m - 1, d).toLocaleDateString("en-US", {
        month: "long",
        day: "numeric",
        year: "numeric",
      });
      headline.textContent = `${formatted} Jobs`;
    }
  }

  function renderCalendar() {
    const year = visibleMonth.getFullYear();
    const month = visibleMonth.getMonth();

    monthLabel.textContent = visibleMonth.toLocaleDateString("en-US", {
      month: "long",
      year: "numeric",
    });

    daysContainer.innerHTML = "";

    const firstDayOfMonth = new Date(year, month, 1).getDay();
    const daysInMonth = new Date(year, month + 1, 0).getDate();

    for (let i = 0; i < firstDayOfMonth; i += 1) {
      const filler = document.createElement("button");
      filler.type = "button";
      filler.className = "home-calendar-day is-empty";
      filler.disabled = true;
      filler.setAttribute("aria-hidden", "true");
      daysContainer.appendChild(filler);
    }

    for (let day = 1; day <= daysInMonth; day += 1) {
      const key = dayKey(year, month, day);
      const button = document.createElement("button");
      button.type = "button";
      button.className = "home-calendar-day";
      button.textContent = String(day);

      if (selectedDays.has(key)) {
        button.classList.add("is-selected");
      }

      button.addEventListener("click", () => {
        if (selectedDays.has(key)) {
          selectedDays.delete(key);
          button.classList.remove("is-selected");
          window.homePageFilter.filterData.selectedDate = null;
          updateHeadline(null);
          window.homePageFilter.filterAndDisplay();
        } else {
          selectedDays.clear();
          daysContainer.querySelectorAll(".home-calendar-day.is-selected").forEach((el) => {
            el.classList.remove("is-selected");
          });
          selectedDays.add(key);
          button.classList.add("is-selected");
          window.homePageFilter.filterData.selectedDate = key;
          updateHeadline(key);
          window.homePageFilter.filterAndDisplay();
        }
      });

      daysContainer.appendChild(button);
    }
  }

  prevButton.addEventListener("click", () => {
    visibleMonth.setMonth(visibleMonth.getMonth() - 1);
    renderCalendar();
  });

  nextButton.addEventListener("click", () => {
    visibleMonth.setMonth(visibleMonth.getMonth() + 1);
    renderCalendar();
  });

  renderCalendar();
  updateHeadline(todayKey());
  // Set initial filter to today's date
  window.homePageFilter.filterData.selectedDate = todayKey();
  window.homePageFilter.filterAndDisplay();
})();
