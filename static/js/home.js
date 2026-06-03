// Shared filter state for home page
window.homePageFilter = {
  filterData: {
    selectedDate: null,
    selectedEmployees: new Set(),
  },
  activityData: {
    events: [],
    businessCenterAddress: "",
    geocodeCache: new Map(),
    mapInstance: null,
    geocoder: null,
    infoWindow: null,
    markers: [],
  },
  currentPage: 1,
  JOBS_PER_PAGE: 5,

  normalizeEmployeeValue: function(value) {
    return String(value || '')
      .trim()
      .toLowerCase()
      .replace(/\s+/g, '-');
  },

  parseEmployeeList: function(value) {
    return String(value || '')
      .split('||')
      .flatMap((entry) => String(entry || '').split(','))
      .map((entry) => this.normalizeEmployeeValue(entry))
      .filter(Boolean);
  },

  matchesSelectedEmployees: function(job) {
    if (this.filterData.selectedEmployees.size === 0) {
      return true;
    }

    const employeeValues = new Set([
      ...this.parseEmployeeList(job.dataset.technicians),
      ...this.parseEmployeeList(job.dataset.assignedEmployee),
    ]);

    for (const selectedEmployee of this.filterData.selectedEmployees) {
      if (employeeValues.has(selectedEmployee)) {
        return true;
      }
    }

    return false;
  },

  getHighlightedJobDates: function() {
    const jobElements = document.querySelectorAll(".home-job");
    const jobsByDate = new Map();

    jobElements.forEach((job) => {
      const jobDate = job.dataset.jobDate;
      const isCompleted = job.classList.contains("is-completed");

      if (!jobDate) {
        return;
      }

      if (!this.matchesSelectedEmployees(job)) {
        return;
      }

      if (!jobsByDate.has(jobDate)) {
        jobsByDate.set(jobDate, { total: 0, completed: 0 });
      }

      const dayStats = jobsByDate.get(jobDate);
      dayStats.total += 1;
      if (isCompleted) {
        dayStats.completed += 1;
      }
    });

    const pendingDates = new Set();
    const completedDates = new Set();

    jobsByDate.forEach((stats, date) => {
      if (stats.completed === stats.total && stats.total > 0) {
        completedDates.add(date);
      } else if (stats.total > 0) {
        pendingDates.add(date);
      }
    });

    return { pending: pendingDates, completed: completedDates };
  },

  filterAndDisplay: function() {
    const jobElements = document.querySelectorAll(".home-job");
    const visibleJobs = Array.from(jobElements).filter((job) => {
      const jobDate = job.dataset.jobDate;
      if (this.filterData.selectedDate && jobDate !== this.filterData.selectedDate) {
        return false;
      }

      if (!this.matchesSelectedEmployees(job)) {
        return false;
      }

      return true;
    });

    const jobsCount = document.getElementById("home-jobs-count");
    if (jobsCount) {
      jobsCount.textContent = String(visibleJobs.length);
    }

    this.currentPage = 1;
    this.updatePaginationLinks(visibleJobs.length);
    this.displayPageContent();
    this.filterCallsNeeded();
    this.filterEstimatesNeeded();
    this.updateActivityCenter();

    if (typeof this.refreshCalendarHighlights === "function") {
      this.refreshCalendarHighlights();
    }
  },

  filterCallsNeeded: function() {
    const callRows = document.querySelectorAll(".home-call-row");
    const emptyMessage = document.getElementById("home-calls-needed-empty");
    const serverEmptyMessage = document.getElementById("home-calls-needed-empty-server");
    let visibleCount = 0;

    callRows.forEach((row) => {
      const assignedEmployee = row.dataset.assignedEmployee;
      const shouldShow = this.filterData.selectedEmployees.size === 0
        || this.filterData.selectedEmployees.has(assignedEmployee);

      row.style.display = shouldShow ? "" : "none";
      if (shouldShow) {
        visibleCount += 1;
      }
    });

    if (serverEmptyMessage) {
      serverEmptyMessage.style.display = callRows.length === 0 ? "block" : "none";
    }

    if (emptyMessage) {
      emptyMessage.style.display = callRows.length > 0 && visibleCount === 0 ? "block" : "none";
    }
  },

  filterEstimatesNeeded: function() {
    const estimateRows = document.querySelectorAll(".home-estimate-cell");
    const emptyMessage = document.getElementById("home-estimates-empty");
    const serverEmptyMessage = document.getElementById("home-estimates-empty-server");
    let visibleCount = 0;

    estimateRows.forEach((row) => {
      const assignedEmployee = row.dataset.assignedEmployee;
      const shouldShow = this.filterData.selectedEmployees.size === 0
        || this.filterData.selectedEmployees.has(assignedEmployee);

      row.style.display = shouldShow ? "" : "none";
      if (shouldShow) {
        visibleCount += 1;
      }
    });

    if (serverEmptyMessage) {
      serverEmptyMessage.style.display = estimateRows.length === 0 ? "block" : "none";
    }

    if (emptyMessage) {
      emptyMessage.style.display = estimateRows.length > 0 && visibleCount === 0 ? "block" : "none";
    }
  },

  updatePaginationLinks: function(visibleJobsCount) {
    const pagesContainer = document.getElementById("home-jobs-pages");
    if (!pagesContainer) {
      return;
    }

    pagesContainer.style.display = visibleJobsCount === 0 ? "none" : "flex";
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
      // Filter by date if a date is selected
      if (this.filterData.selectedDate && jobDate !== this.filterData.selectedDate) {
        return false;
      }

      // Filter by employee if any employees are selected
      if (!this.matchesSelectedEmployees(job)) {
        return false;
      }

      return true;
    }).sort((a, b) => {
      const dateA = a.dataset.jobDate || "9999-12-31";
      const dateB = b.dataset.jobDate || "9999-12-31";
      if (dateA !== dateB) {
        return dateA.localeCompare(dateB);
      }

      const timeA = a.dataset.jobTime || "99:99";
      const timeB = b.dataset.jobTime || "99:99";
      return timeA.localeCompare(timeB);
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
    if (pagesContainer) {
      const pageLinks = pagesContainer.querySelectorAll("a");
      pageLinks.forEach((link) => link.classList.remove("is-active"));
      if (pageLinks[this.currentPage - 1]) {
        pageLinks[this.currentPage - 1].classList.add("is-active");
      }
    }

    // Show/hide empty state message
    const emptyMessage = document.getElementById("home-jobs-empty");
    if (emptyMessage) {
      emptyMessage.style.display = visibleJobs.length === 0 ? "block" : "none";
    }
  },

  updateActivityCenter: async function() {
    // This method is overridden by home_activity_feed.js.
  },
  updateActivityMap: async function(_filteredEvents) {
    const mapContainer = document.getElementById("home-activity-map");
    const mapEmptyMessage = document.getElementById("home-activity-map-empty");
    if (!mapContainer || !mapEmptyMessage) {
      return;
    }

    if (!window.google || !window.google.maps || !this.activityData.mapInstance) {
      mapEmptyMessage.style.display = "block";
      mapContainer.classList.add("is-map-unavailable");
      return;
    }

    mapEmptyMessage.style.display = "none";
    mapContainer.classList.remove("is-map-unavailable");
  },
};

function getHomeSelectedDateFromUrl() {
  const params = new URLSearchParams(window.location.search || '');
  const rawDate = (params.get('selected_date') || '').trim();
  return /^\d{4}-\d{2}-\d{2}$/.test(rawDate) ? rawDate : null;
}

function syncHomeActionFormsSelectedDate() {
  const actionForms = document.querySelectorAll('.home-job-action-form');
  if (!actionForms.length) {
    return;
  }

  actionForms.forEach((form) => {
    form.addEventListener('submit', () => {
      const nextInput = form.querySelector('input[name="next"]');
      if (!nextInput) {
        return;
      }

      const selectedDate = (window.homePageFilter && window.homePageFilter.filterData
        ? window.homePageFilter.filterData.selectedDate
        : null) || null;

      try {
        const nextUrl = new URL(nextInput.value, window.location.origin);
        if (selectedDate) {
          nextUrl.searchParams.set('selected_date', selectedDate);
        } else {
          nextUrl.searchParams.delete('selected_date');
        }
        nextInput.value = `${nextUrl.pathname}${nextUrl.search}${nextUrl.hash}`;
      } catch (_error) {
        // Ignore malformed next URLs and preserve existing behavior.
      }
    });
  });
}

function syncHomeJobsColumnMinHeight() {
  const jobsColumn = document.querySelector(".home-jobs-column");
  const calendar = document.getElementById("home-calendar");
  if (!jobsColumn || !calendar) {
    return;
  }

  if (window.matchMedia("(max-width: 900px)").matches) {
    jobsColumn.style.minHeight = "";
    return;
  }

  jobsColumn.style.minHeight = `${calendar.offsetHeight}px`;
}

function formatHomeJobTimes() {
  const timeElements = document.querySelectorAll('.home-job-time-display');

  timeElements.forEach((element) => {
    const rawTime = (element.dataset.jobTime || '').trim();

    if (!rawTime) {
      element.textContent = 'N/A';
      return;
    }

    const timeMatch = rawTime.match(/^(\d{1,2}):(\d{2})(?::\d{2})?$/);
    if (!timeMatch) {
      element.textContent = rawTime;
      return;
    }

    const hours24 = parseInt(timeMatch[1], 10);
    const minutes = timeMatch[2];
    if (Number.isNaN(hours24) || hours24 < 0 || hours24 > 23) {
      element.textContent = rawTime;
      return;
    }

    const period = hours24 >= 12 ? 'PM' : 'AM';
    const hours12 = hours24 % 12 || 12;
    element.textContent = `${hours12}:${minutes} ${period}`;
  });
}

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
  formatHomeJobTimes();
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
  const initialSelectedKey = getHomeSelectedDateFromUrl() || dayKey(today.getFullYear(), today.getMonth(), today.getDate());
  const selectedDays = new Set([initialSelectedKey]);
  const visibleMonth = new Date();
  const [initialYear, initialMonth] = initialSelectedKey.split('-').map(Number);
  if (!Number.isNaN(initialYear) && !Number.isNaN(initialMonth)) {
    visibleMonth.setFullYear(initialYear, initialMonth - 1, 1);
  } else {
    visibleMonth.setDate(1);
  }

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
    const highlightedJobDates = window.homePageFilter.getHighlightedJobDates();

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

      if (highlightedJobDates.pending.has(key)) {
        button.classList.add("has-jobs");
      } else if (highlightedJobDates.completed.has(key)) {
        button.classList.add("has-jobs-completed");
      }

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

    syncHomeJobsColumnMinHeight();
  }

  prevButton.addEventListener("click", () => {
    visibleMonth.setMonth(visibleMonth.getMonth() - 1);
    renderCalendar();
  });

  nextButton.addEventListener("click", () => {
    visibleMonth.setMonth(visibleMonth.getMonth() + 1);
    renderCalendar();
  });

  window.homePageFilter.refreshCalendarHighlights = renderCalendar;

  renderCalendar();
  updateHeadline(initialSelectedKey);
  window.homePageFilter.filterData.selectedDate = initialSelectedKey;
  window.homePageFilter.filterAndDisplay();
})();

syncHomeActionFormsSelectedDate();
syncHomeJobsColumnMinHeight();
window.addEventListener("resize", syncHomeJobsColumnMinHeight);
