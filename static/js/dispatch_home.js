(() => {
  const dataNode = document.getElementById("dispatch-home-data");
  if (!dataNode) {
    return;
  }

  let dispatchData = {};
  try {
    dispatchData = JSON.parse(dataNode.textContent || "{}");
  } catch (_err) {
    dispatchData = {};
  }

  const state = {
    selectedDate: String(dispatchData.selectedDate || "").trim(),
    pendingJobs: Array.isArray(dispatchData.pendingJobs) ? dispatchData.pendingJobs.slice() : [],
    scheduledJobs: Array.isArray(dispatchData.scheduledJobs) ? dispatchData.scheduledJobs.slice() : [],
    techRows: Array.isArray(dispatchData.techRows) ? dispatchData.techRows.slice() : [],
    activeJobId: "",
    mobileTechId: "",
    dayStartMinutes: Number(dispatchData.dayStartMinutes || 420),
    dayEndMinutes: Number(dispatchData.dayEndMinutes || 1080),
    customersUrl: String(dispatchData.customersUrl || "").trim(),
    defaultVisibleStartMinutes: Number(dispatchData.defaultVisibleStartMinutes || 420),
    hasAppliedInitialTimelineScroll: false,
  };

  if (!state.selectedDate) {
    state.selectedDate = new Date().toISOString().slice(0, 10);
  }

  if (state.techRows.length > 0) {
    state.mobileTechId = String(state.techRows[0].id || "");
  }

  window.homePageFilter = {
    filterData: {
      selectedDate: state.selectedDate,
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
    updateActivityCenter: async function updateActivityCenter() {},
    updateActivityMap: async function updateActivityMap() {},
  };

  const pendingList = document.getElementById("dispatch-pending-list");
  const pendingEmpty = document.getElementById("dispatch-pending-empty");
  const pendingCount = document.getElementById("dispatch-pending-count");
  const techRowsContainer = document.getElementById("dispatch-tech-rows");
  const timeAxis = document.getElementById("dispatch-time-axis");
  const scheduleTitle = document.getElementById("dispatch-schedule-title");
  const mobileTechSelect = document.getElementById("dispatch-mobile-tech-select");
  const scheduleGrid = document.getElementById("dispatch-schedule-grid");

  const modal = document.getElementById("dispatch-assignment-modal");
  const modalCustomer = document.getElementById("dispatch-modal-customer");
  const modalAddress = document.getElementById("dispatch-modal-address");
  const modalServices = document.getElementById("dispatch-modal-services");
  const modalTech = document.getElementById("dispatch-primary-tech");
  const modalDate = document.getElementById("dispatch-scheduled-date");
  const modalTime = document.getElementById("dispatch-scheduled-time");
  const modalSave = document.getElementById("dispatch-modal-save");
  const modalError = document.getElementById("dispatch-modal-error");
  const modalViewJob = document.getElementById("dispatch-modal-view-job");

  const quickModal = document.getElementById("dispatch-quick-assign-modal");
  const quickJobId = document.getElementById("dispatch-quick-job-id");
  const quickEmpty = document.getElementById("dispatch-quick-empty");
  const quickTech = document.getElementById("dispatch-quick-primary-tech");
  const quickDate = document.getElementById("dispatch-quick-date");
  const quickTime = document.getElementById("dispatch-quick-time");
  const quickSave = document.getElementById("dispatch-quick-save");
  const quickCreateNew = document.getElementById("dispatch-quick-create-new");
  const quickError = document.getElementById("dispatch-quick-error");

  state.quickAssign = {
    techId: "",
    minuteOfDay: state.dayStartMinutes,
  };

  function escapeHtml(text) {
    return String(text || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/\"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function statusClass(status) {
    const key = String(status || "").trim().toLowerCase();
    if (key === "pending") return "dispatch-status-pending";
    if (key === "scheduled") return "dispatch-status-scheduled";
    if (key === "en route") return "dispatch-status-en-route";
    if (key === "started") return "dispatch-status-started";
    if (key === "paid") return "dispatch-status-paid";
    if (key === "completed" || key === "complete" || key === "done") return "dispatch-status-completed";
    return "dispatch-status-default";
  }

  function formatDateLabel(isoDate) {
    if (!isoDate) {
      return "Day Schedule";
    }
    const dt = new Date(`${isoDate}T00:00:00`);
    if (Number.isNaN(dt.getTime())) {
      return "Day Schedule";
    }
    return `Day Schedule - ${dt.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric", year: "numeric" })}`;
  }

  function formatTimeLabel(minutes) {
    const h24 = Math.floor(minutes / 60);
    const mins = minutes % 60;
    const period = h24 >= 12 ? "PM" : "AM";
    const h12 = h24 % 12 || 12;
    return `${h12}:00 ${period}`;
  }

  function techDisplayRows() {
    const isMobile = window.matchMedia("(max-width: 900px)").matches;
    if (!isMobile) {
      return state.techRows;
    }
    if (!state.mobileTechId && state.techRows.length) {
      state.mobileTechId = String(state.techRows[0].id || "");
    }
    return state.techRows.filter((row) => String(row.id || "") === String(state.mobileTechId || ""));
  }

  function jobsForDateAndTech(isoDate, techId) {
    return state.scheduledJobs
      .filter((job) => String(job.date_iso || "") === String(isoDate || "") && String(job.primary_technician_id || "") === String(techId || ""))
      .sort((a, b) => Number(a.start_minutes || 0) - Number(b.start_minutes || 0));
  }

  function buildLaneJobs(jobs) {
    const lanes = [];
    const placed = [];

    jobs.forEach((job) => {
      const start = Number(job.start_minutes || 0);
      const duration = Math.max(15, Number(job.duration_minutes || 45));
      const end = start + duration;
      let laneIndex = 0;

      while (laneIndex < lanes.length && lanes[laneIndex] > start) {
        laneIndex += 1;
      }

      if (laneIndex >= lanes.length) {
        lanes.push(end);
      } else {
        lanes[laneIndex] = end;
      }

      placed.push({
        ...job,
        lane: laneIndex,
        end_minutes: end,
      });
    });

    return { placed, laneCount: Math.max(1, lanes.length) };
  }

  function renderPendingQueue() {
    if (!pendingList || !pendingEmpty || !pendingCount) {
      return;
    }

    pendingList.innerHTML = "";
    pendingCount.textContent = String(state.pendingJobs.length);

    if (!state.pendingJobs.length) {
      pendingEmpty.hidden = false;
      return;
    }

    pendingEmpty.hidden = true;
    state.pendingJobs.forEach((job) => {
      const card = document.createElement("button");
      card.type = "button";
      card.className = "dispatch-pending-card dispatch-job-interactive";
      card.dataset.jobId = String(job.id || "");
      card.innerHTML = `
        <p class="dispatch-pending-service">${escapeHtml(job.primary_service_name || "No services added")}</p>
        <div class="dispatch-pending-top">
          <p class="dispatch-pending-customer">${escapeHtml(job.customer_name || "Unknown Customer")}</p>
          <p class="dispatch-pending-meta">Pending ${Number(job.pending_days || 0)} day${Number(job.pending_days || 0) === 1 ? "" : "s"}</p>
        </div>
        ${job.primary_service_category ? `<p class="dispatch-pending-category">${escapeHtml(job.primary_service_category)}</p>` : ""}
        <p class="dispatch-pending-address">${escapeHtml(job.address || "No address")}</p>
      `;

      card.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        openAssignmentModal(job);
      });
      pendingList.appendChild(card);
    });
  }

  function renderMobileTechPicker() {
    if (!mobileTechSelect) {
      return;
    }

    mobileTechSelect.innerHTML = "";
    state.techRows.forEach((tech) => {
      const option = document.createElement("option");
      option.value = String(tech.id || "");
      option.textContent = String(tech.name || "Technician");
      mobileTechSelect.appendChild(option);
    });

    if (state.mobileTechId) {
      mobileTechSelect.value = state.mobileTechId;
    }

    mobileTechSelect.onchange = () => {
      state.mobileTechId = mobileTechSelect.value;
      renderSchedule();
    };
  }

  function renderTimeAxis() {
    if (!timeAxis) {
      return;
    }

    timeAxis.innerHTML = "";

    const nameCell = document.createElement("div");
    nameCell.className = "dispatch-tech-name-cell dispatch-tech-name-cell--axis";
    nameCell.textContent = "Technician";
    timeAxis.appendChild(nameCell);

    const axisTrack = document.createElement("div");
    axisTrack.className = "dispatch-time-axis-track";

    const totalRange = state.dayEndMinutes - state.dayStartMinutes;
    const hourTicks = [];
    for (let minute = state.dayStartMinutes; minute <= state.dayEndMinutes; minute += 60) {
      hourTicks.push(minute);
    }

    hourTicks.forEach((minute, index) => {
      const leftPct = ((minute - state.dayStartMinutes) / totalRange) * 100;

      const marker = document.createElement("span");
      marker.className = "dispatch-time-axis-hour-marker";
      marker.style.left = `${leftPct}%`;
      axisTrack.appendChild(marker);

      const label = document.createElement("span");
      label.className = "dispatch-time-axis-hour-label";
      const isLastTick = index === hourTicks.length - 1;
      const labelMinute = isLastTick ? minute - 30 : minute + 30;
      const labelLeftPct = ((labelMinute - state.dayStartMinutes) / totalRange) * 100;
      label.style.left = `${labelLeftPct}%`;
      label.textContent = formatTimeLabel(minute);
      axisTrack.appendChild(label);
    });

    timeAxis.appendChild(axisTrack);
  }

  function maybeRenderCurrentTimeLine(track) {
    const now = new Date();
    const todayIso = now.toISOString().slice(0, 10);
    if (state.selectedDate !== todayIso) {
      return;
    }

    const nowMinutes = now.getHours() * 60 + now.getMinutes();
    if (nowMinutes < state.dayStartMinutes || nowMinutes > state.dayEndMinutes) {
      return;
    }

    const totalRange = state.dayEndMinutes - state.dayStartMinutes;
    const leftPct = ((nowMinutes - state.dayStartMinutes) / totalRange) * 100;

    const line = document.createElement("div");
    line.className = "dispatch-now-line";
    line.style.left = `${leftPct}%`;
    track.appendChild(line);
  }

  function snapMinuteToSlotStart(minuteOfDay) {
    const slotMinutes = 30;
    const latestSlotStart = Math.max(state.dayStartMinutes, state.dayEndMinutes - slotMinutes);
    const numericMinute = Number(minuteOfDay);
    const clamped = Math.max(
      state.dayStartMinutes,
      Math.min(latestSlotStart, Number.isNaN(numericMinute) ? state.dayStartMinutes : numericMinute)
    );
    const offset = clamped - state.dayStartMinutes;
    return state.dayStartMinutes + Math.floor(offset / slotMinutes) * slotMinutes;
  }

  function prefillNewJobFromEmptySlot(techId, minuteOfDay) {
    if (!state.customersUrl) {
      return;
    }

    const rounded = snapMinuteToSlotStart(minuteOfDay);
    const hh = String(Math.floor(rounded / 60)).padStart(2, "0");
    const mm = String(rounded % 60).padStart(2, "0");
    const target = new URL(state.customersUrl, window.location.origin);
    target.searchParams.set("dispatch_date", state.selectedDate);
    target.searchParams.set("dispatch_time", `${hh}:${mm}`);
    target.searchParams.set("primary_technician_id", String(techId || ""));
    window.location.href = `${target.pathname}${target.search}`;
  }

  function minutesToTimeString(minutesOfDay) {
    const rounded = snapMinuteToSlotStart(minutesOfDay);
    const hh = String(Math.floor(rounded / 60)).padStart(2, "0");
    const mm = String(rounded % 60).padStart(2, "0");
    return `${hh}:${mm}`;
  }

  function ensureTechRow(primaryTechId, assignedEmployeeName) {
    const hasTechRow = state.techRows.some((row) => String(row.id || "") === String(primaryTechId || ""));
    if (hasTechRow || !primaryTechId) {
      return;
    }

    state.techRows.push({
      id: String(primaryTechId || ""),
      name: String(assignedEmployeeName || "Technician"),
      is_active: false,
    });
    state.techRows.sort((a, b) => {
      const aBucket = a.is_active ? 0 : 1;
      const bBucket = b.is_active ? 0 : 1;
      if (aBucket !== bBucket) {
        return aBucket - bBucket;
      }
      return String(a.name || "").localeCompare(String(b.name || ""));
    });
  }

  async function assignJobToSlot(jobId, primaryTechId, scheduledDate, scheduledTime, errorNode, saveButton, saveLabel) {
    const normalizedJobId = String(jobId || "").trim();
    const normalizedTechId = String(primaryTechId || "").trim();
    const normalizedDate = String(scheduledDate || "").trim();
    const normalizedTime = String(scheduledTime || "").trim();
    if (!normalizedJobId || !normalizedTechId || !normalizedDate || !normalizedTime) {
      if (errorNode) {
        errorNode.hidden = false;
        errorNode.textContent = "Primary technician, date, and time are required.";
      }
      return false;
    }

    const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";
    const originalText = saveButton ? saveButton.textContent : "";
    if (saveButton) {
      saveButton.disabled = true;
      saveButton.textContent = saveLabel;
    }

    try {
      const response = await fetch(`/jobs/${encodeURIComponent(normalizedJobId)}/dispatch-assign`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": csrfToken,
        },
        body: JSON.stringify({
          primary_technician_id: normalizedTechId,
          scheduled_date: normalizedDate,
          scheduled_time: normalizedTime,
        }),
      });

      const payload = await response.json();
      if (!response.ok || !payload || !payload.success) {
        throw new Error((payload && payload.error) || "Unable to save assignment.");
      }

      state.pendingJobs = state.pendingJobs.filter((job) => String(job.id || "") !== normalizedJobId);
      upsertScheduledJobFromResponse(payload.job || {});
      ensureTechRow(payload.job.primary_technician_id, payload.job.assigned_employee);
      renderPendingQueue();
      renderMobileTechPicker();
      renderSchedule();
      return true;
    } catch (err) {
      if (errorNode) {
        errorNode.hidden = false;
        errorNode.textContent = String(err && err.message ? err.message : "Unable to save assignment.");
      }
      return false;
    } finally {
      if (saveButton) {
        saveButton.disabled = false;
        saveButton.textContent = originalText;
      }
    }
  }

  function closeQuickAssignModal() {
    if (!quickModal) {
      return;
    }
    quickModal.hidden = true;
    if (quickError) {
      quickError.hidden = true;
      quickError.textContent = "";
    }
  }

  function populateQuickPendingOptions() {
    if (!quickJobId) {
      return;
    }

    quickJobId.innerHTML = "";
    state.pendingJobs.forEach((job) => {
      const option = document.createElement("option");
      option.value = String(job.id || "");
      const service = String(job.primary_service_name || "No service").trim();
      option.textContent = `${String(job.customer_name || "Unknown Customer")} - ${service}`;
      quickJobId.appendChild(option);
    });

    const hasRows = state.pendingJobs.length > 0;
    quickJobId.disabled = !hasRows;
    if (quickSave) {
      quickSave.disabled = !hasRows;
    }
    if (quickEmpty) {
      quickEmpty.hidden = hasRows;
    }
  }

  function openQuickAssignModal(techId, minuteOfDay) {
    if (!quickModal) {
      prefillNewJobFromEmptySlot(techId, minuteOfDay);
      return;
    }

    state.quickAssign.techId = String(techId || "");
    state.quickAssign.minuteOfDay = Number(minuteOfDay || state.dayStartMinutes);

    if (quickTech) {
      quickTech.value = state.quickAssign.techId;
    }
    if (quickDate) {
      quickDate.value = state.selectedDate;
    }
    if (quickTime) {
      quickTime.value = minutesToTimeString(state.quickAssign.minuteOfDay);
    }
    if (quickError) {
      quickError.hidden = true;
      quickError.textContent = "";
    }

    populateQuickPendingOptions();
    quickModal.hidden = false;
  }

  function renderSchedule() {
    if (!techRowsContainer || !scheduleTitle) {
      return;
    }

    scheduleTitle.textContent = formatDateLabel(state.selectedDate);
    techRowsContainer.innerHTML = "";
    const rows = techDisplayRows();

    if (!rows.length) {
      const empty = document.createElement("p");
      empty.className = "dispatch-empty-state";
      empty.textContent = "No technicians available.";
      techRowsContainer.appendChild(empty);
      return;
    }

    const totalRange = state.dayEndMinutes - state.dayStartMinutes;
    const hoverSlotMinutes = 30;
    const noJobsLabelAnchorMinutes = 7 * 60;
    const noJobsLabelLeftPct = Math.max(0, Math.min(100, ((noJobsLabelAnchorMinutes - state.dayStartMinutes) / totalRange) * 100));

    function minuteFromClientX(track, clientX) {
      const rect = track.getBoundingClientRect();
      if (!rect.width) {
        return null;
      }
      const ratio = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
      return state.dayStartMinutes + ratio * totalRange;
    }

    function slotStartFromMinute(minuteOfDay) {
      if (typeof minuteOfDay !== "number" || Number.isNaN(minuteOfDay)) {
        return state.dayStartMinutes;
      }

      const normalized = Math.max(state.dayStartMinutes, Math.min(state.dayEndMinutes, minuteOfDay));
      const offset = normalized - state.dayStartMinutes;
      const slotIndex = Math.floor(offset / hoverSlotMinutes);
      const rawSlotStart = state.dayStartMinutes + slotIndex * hoverSlotMinutes;
      return Math.max(state.dayStartMinutes, Math.min(state.dayEndMinutes - hoverSlotMinutes, rawSlotStart));
    }

    function ensureTrackHoverSlot(track) {
      let slot = track.querySelector(".dispatch-track-hover-slot");
      if (slot) {
        return slot;
      }
      slot = document.createElement("span");
      slot.className = "dispatch-track-hover-slot";
      slot.hidden = true;
      track.appendChild(slot);
      return slot;
    }

    function showTrackHoverSlot(track, minuteOfDay) {
      const slot = ensureTrackHoverSlot(track);
      const slotStart = slotStartFromMinute(minuteOfDay);
      const leftPct = ((slotStart - state.dayStartMinutes) / totalRange) * 100;
      const widthPct = (hoverSlotMinutes / totalRange) * 100;
      slot.style.left = `${leftPct}%`;
      slot.style.width = `${widthPct}%`;
      slot.hidden = false;
    }

    function hideTrackHoverSlot(track) {
      const slot = track.querySelector(".dispatch-track-hover-slot");
      if (slot) {
        slot.hidden = true;
      }
    }

    rows.forEach((tech) => {
      const row = document.createElement("div");
      row.className = "dispatch-tech-row";

      const nameCell = document.createElement("div");
      nameCell.className = "dispatch-tech-name-cell";
      nameCell.innerHTML = `<strong>${escapeHtml(tech.name || "Technician")}</strong>${tech.is_active ? "" : "<span class='dispatch-tech-inactive'>Inactive</span>"}`;
      row.appendChild(nameCell);

      const track = document.createElement("div");
      track.className = "dispatch-tech-track";
      track.dataset.techId = String(tech.id || "");

      const jobs = jobsForDateAndTech(state.selectedDate, tech.id);
      const laidOut = buildLaneJobs(jobs);
      track.style.minHeight = `${Math.max(72, laidOut.laneCount * 34 + 10)}px`;

      if (!jobs.length) {
        const noJobs = document.createElement("span");
        noJobs.className = "dispatch-no-jobs-label";
        noJobs.textContent = "No jobs scheduled";
        noJobs.style.left = `max(0.5rem, ${noJobsLabelLeftPct}%)`;
        track.appendChild(noJobs);
      }

      for (let minute = state.dayStartMinutes; minute <= state.dayEndMinutes; minute += 60) {
        const marker = document.createElement("span");
        marker.className = "dispatch-hour-marker";
        marker.style.left = `${((minute - state.dayStartMinutes) / totalRange) * 100}%`;
        track.appendChild(marker);

        const halfMinute = minute + 30;
        if (halfMinute < state.dayEndMinutes) {
          const halfMarker = document.createElement("span");
          halfMarker.className = "dispatch-half-hour-marker";
          halfMarker.style.left = `${((halfMinute - state.dayStartMinutes) / totalRange) * 100}%`;
          track.appendChild(halfMarker);
        }
      }

      laidOut.placed.forEach((job) => {
        const start = Number(job.start_minutes || state.dayStartMinutes);
        const end = Math.min(state.dayEndMinutes, Number(job.end_minutes || (start + 45)));
        const leftPct = Math.max(0, ((start - state.dayStartMinutes) / totalRange) * 100);
        const widthPct = Math.max(2.5, ((end - start) / totalRange) * 100);

        const block = document.createElement("button");
        block.type = "button";
        block.className = `dispatch-job-block dispatch-job-interactive ${statusClass(job.status)}`;
        block.style.left = `${leftPct}%`;
        block.style.width = `${widthPct}%`;
        block.style.top = `${job.lane * 34 + 4}px`;
        block.title = `${job.customer_name || "Customer"}${job.primary_service_name ? ` - ${job.primary_service_name}` : ""}`;

        const label = widthPct >= 14
          ? `${job.customer_name || "Customer"}${job.primary_service_name ? ` - ${job.primary_service_name}` : ""}`
          : `${job.customer_name || "Customer"}`;
        block.textContent = label;

        block.addEventListener("click", (event) => {
          event.preventDefault();
          event.stopPropagation();
          openAssignmentModal(job);
        });

        track.appendChild(block);
      });

      maybeRenderCurrentTimeLine(track);

      track.addEventListener("click", (event) => {
        if (event.target && typeof event.target.closest === "function" && event.target.closest(".dispatch-job-interactive")) {
          return;
        }

        if (event.target !== track) {
          return;
        }

        const minuteOfDay = minuteFromClientX(track, event.clientX);
        if (minuteOfDay === null) {
          return;
        }
        const snappedMinute = slotStartFromMinute(minuteOfDay);
        showTrackHoverSlot(track, snappedMinute);
        openQuickAssignModal(tech.id, snappedMinute);
      });

      track.addEventListener("mousemove", (event) => {
        if (event.target && typeof event.target.closest === "function" && event.target.closest(".dispatch-job-interactive")) {
          hideTrackHoverSlot(track);
          return;
        }
        const minuteOfDay = minuteFromClientX(track, event.clientX);
        if (minuteOfDay === null) {
          hideTrackHoverSlot(track);
          return;
        }
        showTrackHoverSlot(track, minuteOfDay);
      });

      track.addEventListener("mouseleave", () => {
        hideTrackHoverSlot(track);
      });

      track.addEventListener("touchstart", (event) => {
        if (!event.touches || !event.touches[0]) {
          return;
        }
        const minuteOfDay = minuteFromClientX(track, event.touches[0].clientX);
        if (minuteOfDay === null) {
          return;
        }
        showTrackHoverSlot(track, minuteOfDay);
      }, { passive: true });

      track.addEventListener("touchmove", (event) => {
        if (!event.touches || !event.touches[0]) {
          return;
        }
        const minuteOfDay = minuteFromClientX(track, event.touches[0].clientX);
        if (minuteOfDay === null) {
          return;
        }
        showTrackHoverSlot(track, minuteOfDay);
      }, { passive: true });

      track.addEventListener("touchend", () => {
        window.setTimeout(() => hideTrackHoverSlot(track), 450);
      }, { passive: true });

      row.appendChild(track);
      techRowsContainer.appendChild(row);
    });

    if (!state.hasAppliedInitialTimelineScroll && scheduleGrid) {
      window.requestAnimationFrame(() => {
        const firstTrack = techRowsContainer.querySelector(".dispatch-tech-track");
        if (!firstTrack) {
          return;
        }

        const totalRange = state.dayEndMinutes - state.dayStartMinutes;
        if (totalRange <= 0) {
          return;
        }

        const preferredMinute = Math.max(
          state.dayStartMinutes,
          Math.min(state.dayEndMinutes, state.defaultVisibleStartMinutes)
        );
        const ratio = (preferredMinute - state.dayStartMinutes) / totalRange;
        const timelineWidth = firstTrack.clientWidth;
        const maxScrollLeft = Math.max(0, scheduleGrid.scrollWidth - scheduleGrid.clientWidth);
        const targetScrollLeft = Math.max(0, Math.min(maxScrollLeft, ratio * timelineWidth));

        scheduleGrid.scrollLeft = targetScrollLeft;
        state.hasAppliedInitialTimelineScroll = true;
      });
    }
  }

  function closeAssignmentModal() {
    if (!modal) {
      return;
    }
    modal.hidden = true;
    state.activeJobId = "";
    if (modalError) {
      modalError.hidden = true;
      modalError.textContent = "";
    }
  }

  function openAssignmentModal(job) {
    if (!modal) {
      return;
    }

    state.activeJobId = String(job.id || "");
    if (modalCustomer) {
      modalCustomer.textContent = String(job.customer_name || "Unknown Customer");
    }
    if (modalAddress) {
      modalAddress.textContent = String(job.address || "No address");
    }

    const allServices = Array.isArray(job.all_service_names) ? job.all_service_names.filter(Boolean) : [];
    const additional = Array.isArray(job.additional_services) ? job.additional_services.filter(Boolean) : [];
    const mergedServices = allServices.length ? allServices : [job.primary_service_name].concat(additional);
    if (modalServices) {
      const serviceLabel = mergedServices.filter(Boolean).join(", ");
      modalServices.textContent = serviceLabel || "No services added";
    }

    if (modalTech) {
      modalTech.value = String(job.primary_technician_id || "");
    }
    if (modalDate) {
      modalDate.value = String(job.scheduled_date || job.date_iso || state.selectedDate || "");
    }
    if (modalTime) {
      modalTime.value = String(job.scheduled_time || "");
    }
    if (modalViewJob) {
      modalViewJob.href = String(job.view_url || "#");
    }

    if (modalError) {
      modalError.hidden = true;
      modalError.textContent = "";
    }

    modal.hidden = false;
  }

  function upsertScheduledJobFromResponse(payload) {
    const jobId = String(payload.id || "");
    if (!jobId) {
      return;
    }

    const next = {
      id: jobId,
      customer_name: String(payload.customer_name || "Unknown Customer"),
      primary_service_name: String(payload.primary_service_name || ""),
      primary_service_category: String(payload.primary_service_category || ""),
      status: String(payload.status || "Scheduled"),
      address: String(payload.address || ""),
      date_iso: String(payload.scheduled_date || ""),
      scheduled_time: String(payload.scheduled_time || ""),
      start_minutes: Number(payload.start_minutes || 0),
      duration_minutes: Number(payload.duration_minutes || 45),
      primary_technician_id: String(payload.primary_technician_id || ""),
      assigned_employee: String(payload.assigned_employee || ""),
      view_url: String(payload.view_url || "#"),
      all_service_names: Array.isArray(payload.all_service_names) ? payload.all_service_names : [],
    };

    const idx = state.scheduledJobs.findIndex((row) => String(row.id || "") === jobId);
    if (idx >= 0) {
      state.scheduledJobs[idx] = next;
    } else {
      state.scheduledJobs.push(next);
    }
  }

  async function saveAssignment() {
    if (!state.activeJobId || !modalTech || !modalDate || !modalTime || !modalSave) {
      return;
    }

    const primaryTechId = String(modalTech.value || "").trim();
    const scheduledDate = String(modalDate.value || "").trim();
    const scheduledTime = String(modalTime.value || "").trim();

    if (!primaryTechId || !scheduledDate || !scheduledTime) {
      if (modalError) {
        modalError.hidden = false;
        modalError.textContent = "Primary technician, date, and time are required.";
      }
      return;
    }

    const saved = await assignJobToSlot(
      state.activeJobId,
      primaryTechId,
      scheduledDate,
      scheduledTime,
      modalError,
      modalSave,
      "Saving..."
    );
    if (saved) {
      closeAssignmentModal();
    }
  }

  function initializeAssignmentModal() {
    if (!modal) {
      return;
    }

    modal.querySelectorAll("[data-dispatch-modal-close]").forEach((el) => {
      el.addEventListener("click", closeAssignmentModal);
    });

    if (modalSave) {
      modalSave.addEventListener("click", saveAssignment);
    }

    if (quickModal) {
      quickModal.querySelectorAll("[data-dispatch-quick-close]").forEach((el) => {
        el.addEventListener("click", closeQuickAssignModal);
      });
    }

    if (quickCreateNew) {
      quickCreateNew.addEventListener("click", () => {
        closeQuickAssignModal();
        prefillNewJobFromEmptySlot(state.quickAssign.techId, state.quickAssign.minuteOfDay);
      });
    }

    if (quickSave) {
      quickSave.addEventListener("click", async () => {
        const selectedJobId = String((quickJobId && quickJobId.value) || "").trim();
        const selectedTechId = String((quickTech && quickTech.value) || "").trim();
        const selectedDate = String((quickDate && quickDate.value) || "").trim();
        const selectedTime = String((quickTime && quickTime.value) || "").trim();
        const saved = await assignJobToSlot(
          selectedJobId,
          selectedTechId,
          selectedDate,
          selectedTime,
          quickError,
          quickSave,
          "Assigning..."
        );
        if (saved) {
          closeQuickAssignModal();
        }
      });
    }
  }

  function renderCalendar() {
    const monthLabel = document.getElementById("dispatch-calendar-month-label");
    const daysContainer = document.getElementById("dispatch-calendar-days");
    const prevButton = document.getElementById("dispatch-calendar-prev");
    const nextButton = document.getElementById("dispatch-calendar-next");

    if (!monthLabel || !daysContainer || !prevButton || !nextButton) {
      return;
    }

    const selected = new Date(`${state.selectedDate}T00:00:00`);
    const visibleMonth = Number.isNaN(selected.getTime()) ? new Date() : new Date(selected);
    visibleMonth.setDate(1);

    function dayKey(year, month, day) {
      return `${year}-${String(month + 1).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
    }

    function draw() {
      const year = visibleMonth.getFullYear();
      const month = visibleMonth.getMonth();
      monthLabel.textContent = visibleMonth.toLocaleDateString("en-US", { month: "long", year: "numeric" });
      daysContainer.innerHTML = "";

      const firstDay = new Date(year, month, 1).getDay();
      const daysInMonth = new Date(year, month + 1, 0).getDate();

      for (let i = 0; i < firstDay; i += 1) {
        const filler = document.createElement("button");
        filler.type = "button";
        filler.className = "dispatch-calendar-day is-empty";
        filler.disabled = true;
        daysContainer.appendChild(filler);
      }

      for (let day = 1; day <= daysInMonth; day += 1) {
        const key = dayKey(year, month, day);
        const button = document.createElement("button");
        button.type = "button";
        button.className = "dispatch-calendar-day";
        button.textContent = String(day);

        if (key === state.selectedDate) {
          button.classList.add("is-selected");
        }

        button.addEventListener("click", () => {
          state.selectedDate = key;
          window.homePageFilter.filterData.selectedDate = key;
          const nextUrl = new URL(window.location.href);
          nextUrl.searchParams.set("dispatch_date", key);
          window.history.replaceState({}, "", `${nextUrl.pathname}${nextUrl.search}${nextUrl.hash}`);
          draw();
          renderSchedule();
          window.homePageFilter.updateActivityCenter();
        });

        daysContainer.appendChild(button);
      }
    }

    prevButton.onclick = () => {
      visibleMonth.setMonth(visibleMonth.getMonth() - 1);
      draw();
    };

    nextButton.onclick = () => {
      visibleMonth.setMonth(visibleMonth.getMonth() + 1);
      draw();
    };

    draw();
  }

  function setupMobileSwipes() {
    if (!scheduleGrid || !mobileTechSelect) {
      return;
    }

    let startX = 0;
    scheduleGrid.addEventListener("touchstart", (event) => {
      if (!event.touches || !event.touches[0]) {
        return;
      }
      startX = event.touches[0].clientX;
    }, { passive: true });

    scheduleGrid.addEventListener("touchend", (event) => {
      if (!event.changedTouches || !event.changedTouches[0]) {
        return;
      }
      const deltaX = event.changedTouches[0].clientX - startX;
      if (Math.abs(deltaX) < 40 || !window.matchMedia("(max-width: 900px)").matches) {
        return;
      }

      const options = Array.from(mobileTechSelect.options);
      const currentIndex = options.findIndex((opt) => opt.value === state.mobileTechId);
      if (currentIndex < 0) {
        return;
      }

      const nextIndex = deltaX < 0 ? Math.min(options.length - 1, currentIndex + 1) : Math.max(0, currentIndex - 1);
      if (nextIndex === currentIndex) {
        return;
      }

      state.mobileTechId = options[nextIndex].value;
      mobileTechSelect.value = state.mobileTechId;
      renderSchedule();
    }, { passive: true });
  }

  function addCreatedJobToBoard(card) {
    if (!card || typeof card !== "object") {
      return false;
    }
    const jobId = String(card.id || "").trim();
    if (!jobId) {
      return false;
    }

    // Drop any existing copy so re-creation/duplication never stacks.
    state.pendingJobs = state.pendingJobs.filter((job) => String(job.id || "") !== jobId);
    const existingScheduledIndex = state.scheduledJobs.findIndex((row) => String(row.id || "") === jobId);
    if (existingScheduledIndex >= 0) {
      state.scheduledJobs.splice(existingScheduledIndex, 1);
    }

    const hasTech = !!String(card.primary_technician_id || "").trim();
    const hasDate = !!String(card.scheduled_date || card.date_iso || "").trim();
    const isPending = typeof card.is_pending === "boolean" ? card.is_pending : !(hasTech && hasDate);

    if (isPending) {
      // Newest unassigned job goes to the top of the queue.
      state.pendingJobs.unshift({
        id: jobId,
        customer_name: String(card.customer_name || "Unknown Customer"),
        primary_service_name: String(card.primary_service_name || ""),
        primary_service_category: String(card.primary_service_category || ""),
        pending_days: Number(card.pending_days || 0),
        address: String(card.address || ""),
        status: String(card.status || "Pending"),
        scheduled_date: String(card.scheduled_date || card.date_iso || ""),
        scheduled_time: String(card.scheduled_time || ""),
        primary_technician_id: String(card.primary_technician_id || ""),
        assigned_employee: String(card.assigned_employee || ""),
        additional_services: Array.isArray(card.all_service_names) ? card.all_service_names.slice(1) : [],
        view_url: String(card.view_url || "#"),
      });
    } else {
      upsertScheduledJobFromResponse(card);
      ensureTechRow(card.primary_technician_id, card.assigned_employee);
    }

    renderPendingQueue();
    renderMobileTechPicker();
    renderSchedule();
    return true;
  }

  // Public hook used by the global quick "+ Service Call" modal to refresh the
  // dispatch board live after a job is created, without a full page reload.
  window.kloventDispatchHome = {
    addCreatedJob: addCreatedJobToBoard,
  };

  renderPendingQueue();
  renderTimeAxis();
  renderMobileTechPicker();
  renderSchedule();
  renderCalendar();
  initializeAssignmentModal();
  setupMobileSwipes();

  window.addEventListener("resize", () => {
    renderSchedule();
  });
})();
