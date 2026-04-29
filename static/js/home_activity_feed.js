(function () {
  if (!window.homePageFilter) {
    return;
  }

  function initializeActivityCenterData() {
    const eventsJsonNode = document.getElementById("home-activity-events-data");
    const configJsonNode = document.getElementById("home-activity-config");

    if (eventsJsonNode) {
      try {
        const parsedEvents = JSON.parse(eventsJsonNode.textContent || "[]");
        window.homePageFilter.activityData.events = Array.isArray(parsedEvents) ? parsedEvents : [];
      } catch (_error) {
        window.homePageFilter.activityData.events = [];
      }
    }

    if (configJsonNode) {
      try {
        const parsedConfig = JSON.parse(configJsonNode.textContent || "{}");
        window.homePageFilter.activityData.businessCenterAddress = parsedConfig.business_center_address || "";
      } catch (_error) {
        window.homePageFilter.activityData.businessCenterAddress = "";
      }
    }
  }

  window.homePageFilter.formatActivitySentence = function formatActivitySentence(event) {
    const employee = event.employee || "An employee";
    const action = event.action || "updated";
    const jobTitle = event.job_title || "service";
    const customer = event.customer_name || "Unknown customer";
    const dateTime = event.event_display || "Unknown time";
    return `${employee} ${action} job ${jobTitle} for ${customer} at ${dateTime}`;
  };

  window.homePageFilter.getSelectedActivityDateKey = function getSelectedActivityDateKey() {
    if (this.filterData.selectedDate) {
      return this.filterData.selectedDate;
    }

    const now = new Date();
    const year = now.getFullYear();
    const month = String(now.getMonth() + 1).padStart(2, "0");
    const day = String(now.getDate()).padStart(2, "0");
    return `${year}-${month}-${day}`;
  };

  window.homePageFilter.updateActivityCenter = async function updateActivityCenter() {
    const feedList = document.getElementById("home-activity-feed-list");
    const emptyMessage = document.getElementById("home-activity-empty");
    if (!feedList || !emptyMessage) {
      return;
    }

    const selectedDateKey = this.getSelectedActivityDateKey();
    const selectedEmployees = this.filterData.selectedEmployees;
    const allEvents = Array.isArray(this.activityData.events) ? this.activityData.events : [];

    const filteredEvents = allEvents.filter((event) => {
      const eventDateKey = event.event_date_key || "";
      if (eventDateKey !== selectedDateKey) {
        return false;
      }

      if (selectedEmployees.size > 0 && !selectedEmployees.has(event.employee_key || "")) {
        return false;
      }

      return true;
    }).sort((a, b) => {
      const aIso = a.event_iso || "";
      const bIso = b.event_iso || "";
      return bIso.localeCompare(aIso);
    });

    const latestFifteen = filteredEvents.slice(0, 15);
    feedList.innerHTML = "";

    latestFifteen.forEach((event) => {
      const card = document.createElement("article");
      card.className = "home-activity-card";

      const sentence = document.createElement("p");
      sentence.className = "home-activity-text";
      sentence.textContent = this.formatActivitySentence(event);
      card.appendChild(sentence);

      feedList.appendChild(card);
    });

    emptyMessage.style.display = latestFifteen.length === 0 ? "block" : "none";
    await this.updateActivityMap(filteredEvents);
  };

  initializeActivityCenterData();
  window.homePageFilter.updateActivityCenter();
})();
