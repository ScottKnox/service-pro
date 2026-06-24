(function () {
  "use strict";

  var overlay = document.getElementById("quick-service-call-overlay");
  var trigger = document.getElementById("quick-service-call-trigger");
  if (!overlay || !trigger) {
    return;
  }

  var els = {
    modal: document.getElementById("quick-service-call-modal"),
    close: document.getElementById("qsc-close"),
    form: document.getElementById("qsc-form"),
    searchWrap: document.getElementById("qsc-customer-search-wrap"),
    search: document.getElementById("qsc-customer-search"),
    results: document.getElementById("qsc-customer-results"),
    selectedCustomer: document.getElementById("qsc-selected-customer"),
    selectedName: document.getElementById("qsc-selected-customer-name"),
    selectedPhone: document.getElementById("qsc-selected-customer-phone"),
    changeCustomer: document.getElementById("qsc-change-customer"),
    newCustomer: document.getElementById("qsc-new-customer"),
    ncFirst: document.getElementById("qsc-nc-first"),
    ncLast: document.getElementById("qsc-nc-last"),
    ncPhone: document.getElementById("qsc-nc-phone"),
    ncEmail: document.getElementById("qsc-nc-email"),
    ncPhoneError: document.getElementById("qsc-nc-phone-error"),
    ncEmailError: document.getElementById("qsc-nc-email-error"),
    ncAddr1: document.getElementById("qsc-nc-addr1"),
    ncAddr2: document.getElementById("qsc-nc-addr2"),
    ncCity: document.getElementById("qsc-nc-city"),
    ncState: document.getElementById("qsc-nc-state"),
    ncZip: document.getElementById("qsc-nc-zip"),
    propertyWrap: document.getElementById("qsc-property-wrap"),
    propertySelect: document.getElementById("qsc-property-select"),
    propertyReadonly: document.getElementById("qsc-property-readonly"),
    planBanner: document.getElementById("qsc-plan-banner"),
    planTitle: document.getElementById("qsc-plan-banner-title"),
    planBenefits: document.getElementById("qsc-plan-banner-benefits"),
    hvacWrap: document.getElementById("qsc-hvac-wrap"),
    hvacSelect: document.getElementById("qsc-hvac-select"),
    noteWrap: document.getElementById("qsc-note-wrap"),
    note: document.getElementById("qsc-note"),
    scheduleWrap: document.getElementById("qsc-schedule-wrap"),
    date: document.getElementById("qsc-date"),
    time: document.getElementById("qsc-time"),
    tech: document.getElementById("qsc-tech"),
    error: document.getElementById("qsc-error"),
    submit: document.getElementById("qsc-submit"),
  };

  var state = {
    open: false,
    isNewCustomer: false,
    customer: null,
    customerId: "",
    properties: [],
    selectedPropertyId: "",
    selectedProperty: null,
    activePlan: null,
    systems: [],
    employeesLoaded: false,
    submitting: false,
  };

  var searchDebounce = null;
  var searchAbort = null;

  // Cached on init so every page has the service call (Diagnostics) service ready.
  var cachedServiceCallService = null;
  var serviceCallReady = false;
  var LOGIN_URL = "/login";
  var AUTH_ERROR = "__qsc_auth_expired__";
  var authExpiredHandled = false;
  var EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

  function csrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") || "" : "";
  }

  function isDevMode() {
    var host = (window.location && window.location.hostname) || "";
    return host === "localhost" || host === "127.0.0.1" || host === "0.0.0.0" || host === "";
  }

  function devLog() {
    if (!isDevMode()) return;
    try {
      // eslint-disable-next-line no-console
      console.log.apply(console, arguments);
    } catch (e) {}
  }

  // Detect a 401 or a redirect to the login page on any modal API call.
  function checkAuth(response) {
    var redirectedToLogin =
      response &&
      response.redirected &&
      /\/login(\b|\/|\?|$)/i.test(String(response.url || ""));
    if ((response && response.status === 401) || redirectedToLogin) {
      throw new Error(AUTH_ERROR);
    }
    return response;
  }

  function handleAuthExpired() {
    if (authExpiredHandled) return;
    authExpiredHandled = true;
    closeModal(true);
    showToast("Your session has expired. Please log in again.", true);
    window.setTimeout(function () {
      window.location.href = LOGIN_URL;
    }, 2000);
  }

  function isAuthError(err) {
    return !!(err && err.message === AUTH_ERROR);
  }

  function jsonHeaders(extra) {
    var headers = { "X-Requested-With": "XMLHttpRequest", Accept: "application/json" };
    if (extra) {
      Object.keys(extra).forEach(function (k) {
        headers[k] = extra[k];
      });
    }
    return headers;
  }

  function show(el) {
    if (el) el.hidden = false;
  }
  function hide(el) {
    if (el) el.hidden = true;
  }

  function customerDisplayName(c) {
    if (!c) return "";
    var name = (c.customer_name || "").trim();
    if (!name) {
      name = ((c.first_name || "") + " " + (c.last_name || "")).trim();
    }
    if (!name) {
      name = (c.company || "").trim();
    }
    return name || "Unnamed customer";
  }

  function customerSubLine(c) {
    var company = (c.company || "").trim();
    return company;
  }

  function normalizeProperties(props) {
    if (!Array.isArray(props)) return [];
    return props
      .map(function (p) {
        return {
          property_id: String((p && p.property_id) || "").trim(),
          property_name: String((p && p.property_name) || "").trim() || "Property",
          address_line_1: String((p && p.address_line_1) || "").trim(),
          address_line_2: String((p && p.address_line_2) || "").trim(),
          city: String((p && p.city) || "").trim(),
          state: String((p && p.state) || "").trim(),
          zip_code: String((p && p.zip_code) || "").trim(),
        };
      })
      .filter(function (p) {
        return p.property_id;
      });
  }

  /* ----------------------------- Open / Close ----------------------------- */

  function openModal() {
    // Always start from a clean slate so a previous (e.g. just-created) job
    // never leaks its customer, schedule, or button state into a new one.
    resetForm();
    state.open = true;
    overlay.hidden = false;
    document.body.style.overflow = "hidden";
    window.setTimeout(function () {
      if (els.search) els.search.focus();
    }, 30);
    if (!state.employeesLoaded) {
      loadEmployees();
    }
  }

  function isDirty() {
    if (state.customer || state.isNewCustomer) return true;
    if (els.search && els.search.value.trim()) return true;
    var newCustomerInputs = [
      els.ncFirst,
      els.ncLast,
      els.ncPhone,
      els.ncEmail,
      els.ncAddr1,
      els.ncAddr2,
      els.ncCity,
      els.ncState,
      els.ncZip,
    ];
    var anyFilled = newCustomerInputs.some(function (i) {
      return i && i.value.trim();
    });
    if (anyFilled) return true;
    if (els.note && els.note.value.trim()) return true;
    if (els.date && els.date.value) return true;
    if (els.time && els.time.value) return true;
    if (els.tech && els.tech.value) return true;
    return false;
  }

  function closeModal(force) {
    if (!force && isDirty()) {
      var keep = !window.confirm("Discard this service call?");
      if (keep) return;
    }
    state.open = false;
    overlay.hidden = true;
    document.body.style.overflow = "";
    resetForm();
  }

  function resetForm() {
    state.isNewCustomer = false;
    state.customer = null;
    state.customerId = "";
    state.properties = [];
    state.selectedPropertyId = "";
    state.selectedProperty = null;
    state.activePlan = null;
    state.systems = [];
    state.submitting = false;

    if (els.form) els.form.reset();
    if (els.search) els.search.value = "";
    if (els.search) els.search.disabled = false;
    hide(els.results);
    if (els.results) els.results.innerHTML = "";
    show(els.searchWrap);
    hide(els.selectedCustomer);
    hide(els.newCustomer);
    hide(els.propertyWrap);
    hide(els.propertyReadonly);
    hide(els.planBanner);
    hide(els.hvacWrap);
    hide(els.noteWrap);
    hide(els.scheduleWrap);
    hide(els.error);
    if (els.error) els.error.textContent = "";
    if (els.propertySelect) els.propertySelect.innerHTML = "";
    setFieldError(els.ncPhone, els.ncPhoneError, "");
    setFieldError(els.ncEmail, els.ncEmailError, "");
    clearNoPropertyMessage();
    resetHvacSelect();
    if (els.submit) els.submit.textContent = "Create Service Call";
    updateSubmitState();
  }

  function resetHvacSelect() {
    if (!els.hvacSelect) return;
    els.hvacSelect.innerHTML = "";
    var opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "Not sure / Multiple systems";
    els.hvacSelect.appendChild(opt);
  }

  function setError(message) {
    if (!els.error) return;
    if (message) {
      els.error.textContent = message;
      show(els.error);
    } else {
      els.error.textContent = "";
      hide(els.error);
    }
  }

  // Inline notice shown beneath the locked customer when they have no
  // properties on file. Includes a deep link to add one first.
  function showNoPropertyMessage(customerId) {
    var host = els.selectedCustomer;
    if (!host) return;
    clearNoPropertyMessage();

    var wrap = document.createElement("div");
    wrap.className = "qsc-no-property";
    wrap.id = "qsc-no-property";

    var text = document.createElement("p");
    text.className = "qsc-no-property-text";
    text.textContent = "This customer has no properties on file.";
    wrap.appendChild(text);

    var id = String(customerId || "").trim();
    if (id) {
      var link = document.createElement("a");
      link.className = "qsc-no-property-link";
      link.href = "/customers/" + encodeURIComponent(id) + "#properties";
      link.textContent = "Add a property first";
      wrap.appendChild(link);
    }

    host.parentNode.insertBefore(wrap, host.nextSibling);
  }

  function clearNoPropertyMessage() {
    var existing = document.getElementById("qsc-no-property");
    if (existing && existing.parentNode) {
      existing.parentNode.removeChild(existing);
    }
  }

  /* ----------------------------- Customer search ----------------------------- */

  function runSearch(term) {
    if (searchAbort) {
      try {
        searchAbort.abort();
      } catch (e) {}
    }
    searchAbort = typeof AbortController !== "undefined" ? new AbortController() : null;
    var opts = { headers: jsonHeaders(), credentials: "same-origin" };
    if (searchAbort) opts.signal = searchAbort.signal;

    fetch("/api/customers/search?q=" + encodeURIComponent(term), opts)
      .then(checkAuth)
      .then(function (r) {
        return r.ok ? r.json() : { data: [] };
      })
      .then(function (payload) {
        var list = payload && Array.isArray(payload.data) ? payload.data : [];
        renderResults(list);
      })
      .catch(function (err) {
        if (isAuthError(err)) {
          handleAuthExpired();
        }
        /* aborted or network error - ignore */
      });
  }

  function renderResults(customers) {
    if (!els.results) return;
    els.results.innerHTML = "";

    if (!customers.length) {
      var empty = document.createElement("div");
      empty.className = "qsc-result-empty";
      empty.appendChild(document.createTextNode("No customer found — "));
      var addBtn = document.createElement("button");
      addBtn.type = "button";
      addBtn.textContent = "add them now";
      addBtn.addEventListener("click", function () {
        startNewCustomer();
      });
      empty.appendChild(addBtn);
      els.results.appendChild(empty);
      show(els.results);
      return;
    }

    customers.forEach(function (c) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "qsc-result";
      btn.setAttribute("role", "option");

      var nameLine = document.createElement("div");
      nameLine.className = "qsc-result-name";
      var name = customerDisplayName(c);
      var company = customerSubLine(c);
      nameLine.textContent = company ? name + " · " + company : name;

      var phoneLine = document.createElement("div");
      phoneLine.className = "qsc-result-phone";
      phoneLine.textContent = (c.phone || "").trim() || "No phone on file";

      btn.appendChild(nameLine);
      btn.appendChild(phoneLine);
      btn.addEventListener("click", function () {
        selectCustomer(c);
      });
      els.results.appendChild(btn);
    });
    show(els.results);
  }

  function selectCustomer(customer) {
    state.isNewCustomer = false;
    state.customer = customer;
    state.customerId = String(customer._id || customer.id || "").trim();
    hide(els.results);
    hide(els.searchWrap);
    hide(els.newCustomer);

    els.selectedName.textContent = customerDisplayName(customer);
    els.selectedPhone.textContent = (customer.phone || "").trim() || "No phone on file";
    show(els.selectedCustomer);

    state.properties = normalizeProperties(customer.properties);
    setupPropertySelection();
    show(els.noteWrap);
    show(els.scheduleWrap);
    updateSubmitState();
  }

  function startNewCustomer() {
    state.isNewCustomer = true;
    state.customer = null;
    state.customerId = "";
    state.properties = [];
    state.selectedPropertyId = "";
    state.selectedProperty = null;
    state.activePlan = null;
    state.systems = [];

    hide(els.results);
    hide(els.selectedCustomer);
    clearNoPropertyMessage();
    show(els.searchWrap);
    if (els.search) els.search.value = "";
    show(els.newCustomer);
    // No property/hvac until the customer is created on submit.
    hide(els.propertyWrap);
    hide(els.planBanner);
    hide(els.hvacWrap);
    show(els.noteWrap);
    show(els.scheduleWrap);
    window.setTimeout(function () {
      if (els.ncFirst) els.ncFirst.focus();
    }, 20);
    updateSubmitState();
  }

  function changeCustomer() {
    state.customer = null;
    state.customerId = "";
    state.properties = [];
    state.selectedPropertyId = "";
    state.selectedProperty = null;
    state.activePlan = null;
    state.systems = [];
    hide(els.selectedCustomer);
    hide(els.propertyWrap);
    hide(els.propertyReadonly);
    hide(els.planBanner);
    hide(els.hvacWrap);
    clearNoPropertyMessage();
    show(els.searchWrap);
    if (els.search) {
      els.search.value = "";
      els.search.focus();
    }
    updateSubmitState();
  }

  /* ----------------------------- Property selection ----------------------------- */

  function setupPropertySelection() {
    hide(els.propertyReadonly);
    clearNoPropertyMessage();
    if (els.propertySelect) els.propertySelect.innerHTML = "";

    if (!state.properties.length) {
      hide(els.propertyWrap);
      state.selectedPropertyId = "";
      state.selectedProperty = null;
      setError("");
      // Returning customer with no properties: block submit and offer a link.
      if (!state.isNewCustomer) {
        showNoPropertyMessage(state.customerId);
      }
      updateSubmitState();
      return;
    }

    setError("");

    if (state.properties.length === 1) {
      var p = state.properties[0];
      state.selectedPropertyId = p.property_id;
      state.selectedProperty = p;
      show(els.propertyWrap);
      hide(els.propertySelect);
      els.propertyReadonly.textContent =
        "Property: " + p.property_name + (p.address_line_1 ? " — " + p.address_line_1 : "");
      show(els.propertyReadonly);
      onPropertyChosen();
      return;
    }

    // Multiple properties -> dropdown
    show(els.propertyWrap);
    show(els.propertySelect);
    var placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "Select a property…";
    els.propertySelect.appendChild(placeholder);
    state.properties.forEach(function (p) {
      var opt = document.createElement("option");
      opt.value = p.property_id;
      opt.textContent = p.property_name + (p.address_line_1 ? " — " + p.address_line_1 : "");
      els.propertySelect.appendChild(opt);
    });
    state.selectedPropertyId = "";
    state.selectedProperty = null;
    updateSubmitState();
  }

  function onPropertyChosen() {
    if (!state.selectedPropertyId) {
      hide(els.planBanner);
      hide(els.hvacWrap);
      updateSubmitState();
      return;
    }
    loadActivePlan(state.selectedPropertyId);
    loadSystems(state.selectedPropertyId);
    updateSubmitState();
  }

  function loadActivePlan(propertyId) {
    fetch("/api/properties/" + encodeURIComponent(propertyId) + "/active-plan", {
      headers: jsonHeaders(),
      credentials: "same-origin",
    })
      .then(checkAuth)
      .then(function (r) {
        return r.ok ? r.json() : { data: null };
      })
      .then(function (payload) {
        var plan = payload && payload.data ? payload.data : null;
        // Guard against race conditions if the property changed meanwhile.
        if (propertyId !== state.selectedPropertyId) return;
        state.activePlan = plan;
        renderPlanBanner(plan);
      })
      .catch(function (err) {
        if (isAuthError(err)) {
          handleAuthExpired();
          return;
        }
        renderPlanBanner(null);
      });
  }

  function renderPlanBanner(plan) {
    if (!plan) {
      hide(els.planBanner);
      return;
    }
    var snapshot = plan.template_snapshot || {};
    var tierName = (snapshot.name || plan.plan_tier || plan.name || "Maintenance").toString().trim() || "Maintenance";
    els.planTitle.textContent = tierName + " Maintenance Plan — Active";

    var benefits = [];
    if (snapshot.diagnostic_fee_waived) benefits.push("Diagnostic fee waived");
    var pct = parseFloat(snapshot.repair_discount_pct || 0);
    if (pct > 0) {
      var pctText = Number.isInteger(pct) ? pct.toString() : pct.toFixed(1).replace(/\.0$/, "");
      benefits.push(pctText + "% repair discount");
    }
    if (snapshot.priority_scheduling) benefits.push("Priority scheduling");
    if (snapshot.emergency_service) benefits.push("Emergency service");
    if (Array.isArray(snapshot.custom_benefits)) {
      snapshot.custom_benefits.forEach(function (b) {
        var text = String(b || "").trim();
        if (text) benefits.push(text);
      });
    }

    els.planBenefits.textContent = benefits.join(" · ");
    els.planBenefits.style.display = benefits.length ? "" : "none";
    show(els.planBanner);
  }

  function loadSystems(propertyId) {
    fetch("/api/properties/" + encodeURIComponent(propertyId) + "/systems", {
      headers: jsonHeaders(),
      credentials: "same-origin",
    })
      .then(checkAuth)
      .then(function (r) {
        return r.ok ? r.json() : { data: [] };
      })
      .then(function (payload) {
        if (propertyId !== state.selectedPropertyId) return;
        var systems = payload && Array.isArray(payload.data) ? payload.data : [];
        state.systems = systems;
        renderSystems(systems);
      })
      .catch(function (err) {
        if (isAuthError(err)) {
          handleAuthExpired();
          return;
        }
        renderSystems([]);
      });
  }

  function renderSystems(systems) {
    resetHvacSelect();
    if (state.isNewCustomer || !systems.length) {
      hide(els.hvacWrap);
      return;
    }
    systems.forEach(function (s) {
      var opt = document.createElement("option");
      opt.value = String(s._id || "").trim();
      var parts = [];
      if (s.system_nickname) parts.push(s.system_nickname);
      if (s.system_type) parts.push(s.system_type);
      if (s.system_tonnage) parts.push(s.system_tonnage);
      opt.textContent = parts.length ? parts.join(" — ") : s.system_type || "HVAC System";
      opt.setAttribute("data-name", opt.textContent);
      els.hvacSelect.appendChild(opt);
    });
    show(els.hvacWrap);
  }

  /* ----------------------------- Employees ----------------------------- */

  function loadEmployees() {
    fetch("/api/employees/active", { headers: jsonHeaders(), credentials: "same-origin" })
      .then(checkAuth)
      .then(function (r) {
        return r.ok ? r.json() : { employees: [] };
      })
      .then(function (payload) {
        state.employeesLoaded = true;
        var employees = payload && Array.isArray(payload.employees) ? payload.employees : [];
        employees.forEach(function (emp) {
          var opt = document.createElement("option");
          opt.value = String(emp.id || "").trim();
          opt.textContent = String(emp.name || "").trim();
          els.tech.appendChild(opt);
        });
      })
      .catch(function (err) {
        if (isAuthError(err)) {
          handleAuthExpired();
          return;
        }
        state.employeesLoaded = true;
      });
  }

  /* ----------------------------- Submit ----------------------------- */

  function setFieldError(input, errorEl, message) {
    if (errorEl) {
      errorEl.textContent = message || "";
      if (message) {
        errorEl.classList.add("is-visible");
      } else {
        errorEl.classList.remove("is-visible");
      }
    }
    if (input) {
      if (message) {
        input.classList.add("qsc-input-error");
        input.setAttribute("aria-invalid", "true");
      } else {
        input.classList.remove("qsc-input-error");
        input.removeAttribute("aria-invalid");
      }
    }
  }

  // Phone is required for a new customer. It's valid once it contains a full
  // 10-digit US number (the input auto-hyphenates via the global formatter).
  function validateNewCustomerPhone(showError) {
    if (!els.ncPhone) return true;
    var digits = els.ncPhone.value.replace(/\D/g, "");
    if (digits.length > 10 && digits.charAt(0) === "1") {
      digits = digits.slice(1);
    }
    var valid = digits.length === 10;
    if (showError) {
      if (!els.ncPhone.value.trim()) {
        setFieldError(els.ncPhone, els.ncPhoneError, "");
      } else {
        setFieldError(els.ncPhone, els.ncPhoneError, valid ? "" : "Enter a valid phone number.");
      }
    }
    return valid;
  }

  // Email is optional, but if provided it must be a valid address.
  function validateNewCustomerEmail(showError) {
    if (!els.ncEmail) return true;
    var value = els.ncEmail.value.trim();
    var valid = value.length === 0 || EMAIL_PATTERN.test(value);
    if (showError) {
      setFieldError(els.ncEmail, els.ncEmailError, valid ? "" : "Enter a valid email address.");
    }
    return valid;
  }

  function canSubmit() {
    if (state.submitting) return false;
    if (state.isNewCustomer) {
      return !!(
        els.ncFirst.value.trim() &&
        els.ncLast.value.trim() &&
        els.ncPhone.value.trim() &&
        validateNewCustomerPhone(false) &&
        validateNewCustomerEmail(false) &&
        els.ncAddr1.value.trim() &&
        els.ncCity.value.trim() &&
        els.ncState.value.trim() &&
        els.ncZip.value.trim()
      );
    }
    return !!(state.customerId && state.selectedPropertyId);
  }

  function updateSubmitState() {
    if (els.submit) els.submit.disabled = !canSubmit();
  }

  function formEncode(obj) {
    var pairs = [];
    Object.keys(obj).forEach(function (key) {
      var value = obj[key];
      if (Array.isArray(value)) {
        value.forEach(function (v) {
          pairs.push(encodeURIComponent(key) + "=" + encodeURIComponent(v));
        });
      } else {
        pairs.push(encodeURIComponent(key) + "=" + encodeURIComponent(value == null ? "" : value));
      }
    });
    return pairs.join("&");
  }

  function postForm(url, dataObj) {
    return fetch(url, {
      method: "POST",
      headers: jsonHeaders({
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-CSRFToken": csrfToken(),
      }),
      credentials: "same-origin",
      body: formEncode(dataObj),
    }).then(checkAuth);
  }

  function createCustomer() {
    return postForm("/customers/add", {
      first_name: els.ncFirst.value.trim(),
      last_name: els.ncLast.value.trim(),
      company: "",
      customer_type: "Residential",
      phone: els.ncPhone.value.trim(),
      email: els.ncEmail.value.trim(),
      address_line_1: els.ncAddr1.value.trim(),
      address_line_2: els.ncAddr2.value.trim(),
      city: els.ncCity.value.trim(),
      state: els.ncState.value.trim().toUpperCase(),
      zip_code: els.ncZip.value.trim(),
    }).then(function (r) {
      return r.json().then(function (body) {
        if (!r.ok || !body.success) {
          throw new Error(body.error || "Could not create the customer.");
        }
        return body;
      });
    });
  }

  function setServiceCallAvailability(ready) {
    serviceCallReady = !!ready;
    if (!trigger) return;
    if (serviceCallReady) {
      trigger.disabled = false;
      trigger.removeAttribute("aria-disabled");
      trigger.removeAttribute("title");
    } else {
      trigger.disabled = true;
      trigger.setAttribute("aria-disabled", "true");
      trigger.title = "Set up a Diagnostics service in your price book to enable quick job creation.";
    }
  }

  // Fetch + cache the first Diagnostics service on every page load. When none
  // exists, the "+ Service Call" button is disabled with an explanatory tooltip.
  function initServiceCallService() {
    fetch("/api/services?type=Diagnostics&limit=1", {
      headers: jsonHeaders(),
      credentials: "same-origin",
    })
      .then(checkAuth)
      .then(function (r) {
        return r.ok ? r.json() : { services: [] };
      })
      .then(function (payload) {
        var services = payload && Array.isArray(payload.services) ? payload.services : [];
        if (services.length) {
          cachedServiceCallService = services[0];
          setServiceCallAvailability(true);
        } else {
          cachedServiceCallService = null;
          setServiceCallAvailability(false);
        }
      })
      .catch(function (err) {
        if (isAuthError(err)) {
          // Session expired on load: leave the button as-is so a normal click
          // routes through the standard auth-expiry handling.
          return;
        }
        cachedServiceCallService = null;
        setServiceCallAvailability(false);
      });
  }

  function createJob(customerId, property, diagnostics) {
    var systemId = els.hvacSelect && !els.hvacWrap.hidden ? els.hvacSelect.value.trim() : "";
    var payload = {
      job_property_id: property.property_id,
      "service_code[]": [diagnostics.id],
      "service_price[]": [diagnostics.standard_price || ""],
      "service_hours[]": [diagnostics.estimated_hours || ""],
      "service_hvac_system_id[]": [systemId],
      job_address_line_1: property.address_line_1 || "",
      job_address_line_2: property.address_line_2 || "",
      job_city: property.city || "",
      job_state: property.state || "",
      job_zip_code: property.zip_code || "",
      job_date: els.date.value || "",
      job_time: els.time.value || "",
      primary_technician_id: els.tech.value || "",
      internal_note: els.note.value.trim(),
    };
    return postForm("/customers/" + encodeURIComponent(customerId) + "/jobs/create", payload).then(function (r) {
      // Dev-only inspection of the exact payload sent to the job endpoint.
      devLog("[quick-service-call] create job payload", payload);
      return r.json().then(function (body) {
        if (!r.ok || !body.success) {
          throw new Error(body.error || "Could not create the service call.");
        }
        return body;
      });
    });
  }

  function handleSubmit(event) {
    event.preventDefault();
    if (state.isNewCustomer) {
      var phoneValid = validateNewCustomerPhone(true);
      var emailValid = validateNewCustomerEmail(true);
      if (!phoneValid) {
        if (els.ncPhone) els.ncPhone.focus();
        return;
      }
      if (!emailValid) {
        if (els.ncEmail) els.ncEmail.focus();
        return;
      }
    }
    if (!canSubmit()) return;

    // Edge case: no service call (Diagnostics) service exists in the price book.
    if (!cachedServiceCallService) {
      setError(
        "No service call service found in your price book. Please add a Diagnostics type service before creating service calls."
      );
      return;
    }

    setError("");
    state.submitting = true;
    updateSubmitState();
    els.submit.textContent = "Creating…";

    var ensureCustomer;
    if (state.isNewCustomer) {
      ensureCustomer = createCustomer().then(function (body) {
        state.customerId = body.customer_id;
        var props = normalizeProperties(body.properties);
        state.selectedProperty = props.length ? props[0] : null;
        if (!state.selectedProperty) {
          throw new Error("The new customer was created without a property.");
        }
        state.selectedPropertyId = state.selectedProperty.property_id;
      });
    } else {
      ensureCustomer = Promise.resolve();
    }

    ensureCustomer
      .then(function () {
        return createJob(state.customerId, state.selectedProperty, cachedServiceCallService);
      })
      .then(function (body) {
        finishSuccess(body);
      })
      .catch(function (err) {
        if (isAuthError(err)) {
          handleAuthExpired();
          return;
        }
        state.submitting = false;
        els.submit.textContent = "Create Service Call";
        updateSubmitState();
        setError((err && err.message) || "Something went wrong. Please try again.");
      });
  }

  function finishSuccess(body) {
    var techName = "";
    if (els.tech && els.tech.value) {
      var opt = els.tech.options[els.tech.selectedIndex];
      techName = opt ? opt.textContent.trim() : "";
    }
    var dateValue = els.date ? els.date.value : "";
    var message = "Service call created successfully.";
    if (techName && dateValue) {
      message += " Assigned to " + techName + " on " + formatDate(dateValue) + ".";
    }

    var card = body && body.dispatch_card ? body.dispatch_card : null;
    var dispatchHome = window.kloventDispatchHome;
    var canLiveUpdate = !!(dispatchHome && typeof dispatchHome.addCreatedJob === "function");

    closeModal(true);
    showToast(message, false);

    try {
      document.dispatchEvent(
        new CustomEvent("klovent:service-call-created", { detail: { card: card } })
      );
    } catch (e) {}

    // On the dispatch home view, update the board in place (no full reload).
    // Anywhere else, the toast is the only feedback needed.
    if (canLiveUpdate && card) {
      dispatchHome.addCreatedJob(card);
    }
  }

  function formatDate(value) {
    var parts = String(value || "").split("-");
    if (parts.length === 3) {
      return parts[1] + "/" + parts[2] + "/" + parts[0];
    }
    return value;
  }

  /* ----------------------------- Toast ----------------------------- */

  function showToast(message, isError) {
    var toast = document.createElement("div");
    toast.className = "qsc-toast" + (isError ? " is-error" : "");
    toast.textContent = message;
    document.body.appendChild(toast);
    window.requestAnimationFrame(function () {
      toast.classList.add("is-visible");
    });
    window.setTimeout(function () {
      toast.classList.remove("is-visible");
      window.setTimeout(function () {
        if (toast.parentNode) toast.parentNode.removeChild(toast);
      }, 250);
    }, 4000);
  }

  /* ----------------------------- Wiring ----------------------------- */

  trigger.addEventListener("click", function () {
    openModal();
  });
  if (els.close) els.close.addEventListener("click", function () {
    closeModal(false);
  });

  overlay.addEventListener("mousedown", function (event) {
    if (event.target === overlay) {
      closeModal(false);
    }
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && state.open) {
      closeModal(false);
    }
  });

  if (els.search) {
    els.search.addEventListener("input", function () {
      var term = els.search.value.trim();
      if (searchDebounce) window.clearTimeout(searchDebounce);
      if (term.length < 2) {
        hide(els.results);
        if (els.results) els.results.innerHTML = "";
        return;
      }
      searchDebounce = window.setTimeout(function () {
        runSearch(term);
      }, 300);
    });
  }

  if (els.changeCustomer) els.changeCustomer.addEventListener("click", changeCustomer);

  if (els.propertySelect) {
    els.propertySelect.addEventListener("change", function () {
      state.selectedPropertyId = els.propertySelect.value.trim();
      state.selectedProperty = state.properties.filter(function (p) {
        return p.property_id === state.selectedPropertyId;
      })[0] || null;
      onPropertyChosen();
    });
  }

  // Keep submit button state in sync as the user fills fields.
  [
    els.ncFirst,
    els.ncLast,
    els.ncPhone,
    els.ncAddr1,
    els.ncCity,
    els.ncState,
    els.ncZip,
  ].forEach(function (input) {
    if (input) input.addEventListener("input", updateSubmitState);
  });

  if (els.ncPhone) {
    // Clear an existing error live as the number becomes valid; only surface a
    // new error once the field loses focus so we don't nag mid-typing.
    els.ncPhone.addEventListener("input", function () {
      if (els.ncPhoneError && els.ncPhoneError.classList.contains("is-visible")) {
        validateNewCustomerPhone(true);
      }
    });
    els.ncPhone.addEventListener("blur", function () {
      validateNewCustomerPhone(true);
    });
  }

  if (els.ncEmail) {
    els.ncEmail.addEventListener("input", function () {
      validateNewCustomerEmail(true);
      updateSubmitState();
    });
    els.ncEmail.addEventListener("blur", function () {
      validateNewCustomerEmail(true);
    });
  }

  if (els.form) els.form.addEventListener("submit", handleSubmit);

  // Cache the Diagnostics service (and gate the button) as soon as the page loads.
  initServiceCallService();
})();
