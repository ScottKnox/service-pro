(function () {
  "use strict";

  const DATA_URL = "/reporting/revenue/data";

  /* ── DOM refs ───────────────────────────── */
  const startInput = document.getElementById("rev-start-date");
  const endInput = document.getElementById("rev-end-date");
  const timeframeBtns = document.querySelectorAll(".rev-timeframe-btn");
  const serviceBarsEl = document.getElementById("rev-service-bars");
  const equipmentBarsEl = document.getElementById("rev-equipment-bars");
  const employeeDonutEl = document.getElementById("rev-employee-donut");
  const employeeLegendEl = document.getElementById("rev-employee-legend");

  /* ── Helpers ────────────────────────────── */
  function isoDate(date) {
    return date.toISOString().slice(0, 10);
  }

  function today() {
    return new Date();
  }

  function buildRangeLabel(startIso, endIso) {
    var opts = { month: "short", day: "numeric", timeZone: "UTC" };
    var s = new Date(startIso + "T00:00:00Z").toLocaleDateString("en-US", opts);
    var e = new Date(endIso + "T00:00:00Z").toLocaleDateString("en-US", opts);
    return s === e ? s : s + " \u2013 " + e;
  }

  function formatCurrency(amount) {
    return "$" + Number(amount).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function formatPct(pct) {
    if (pct === null || pct === undefined) return null;
    const sign = pct >= 0 ? "+" : "";
    return sign + Number(pct).toFixed(1) + "% vs last year";
  }

  function formatMomPct(pct) {
    if (pct === null || pct === undefined) return null;
    const sign = pct >= 0 ? "+" : "";
    return sign + Number(pct).toFixed(1) + "%";
  }

  /* ── Default date range (7 days) ───────── */
  function setDateRange(days) {
    const end = today();
    const start = new Date(end);
    start.setDate(start.getDate() - (days - 1));
    startInput.value = isoDate(start);
    endInput.value = isoDate(end);
  }

  /* ── Timeframe button state ─────────────── */
  function setActiveBtn(btn) {
    timeframeBtns.forEach(function (b) { b.classList.remove("is-active"); });
    if (btn) btn.classList.add("is-active");
  }

  /* ── KPI card update ────────────────────── */
  function setKpi(valueId, growthId, value, pct, isCurrency) {
    const valEl = document.getElementById(valueId);
    const growthEl = document.getElementById(growthId);

    if (valEl) {
      valEl.textContent = isCurrency ? formatCurrency(value) : String(value);
    }

    if (growthEl) {
      const txt = formatPct(pct);
      if (txt) {
        growthEl.textContent = txt;
        growthEl.className = "rev-kpi-growth " + (pct >= 0 ? "is-up" : "is-down");
      } else {
        growthEl.textContent = "No prior‑year data";
        growthEl.className = "rev-kpi-growth";
      }
    }
  }

  function setMomKpi(pct, prevPct) {
    const valEl = document.getElementById("rev-mom-growth");
    const subEl = document.getElementById("rev-mom-growth-sub");

    if (valEl) {
      if (pct === null || pct === undefined) {
        valEl.textContent = "—";
      } else {
        valEl.textContent = formatMomPct(pct);
      }
    }

    if (subEl) {
      if (prevPct === null || prevPct === undefined) {
        subEl.textContent = "No prior‑month data";
        subEl.className = "rev-kpi-growth";
      } else {
        const diff = pct !== null ? pct - prevPct : null;
        if (diff !== null) {
          const sign = diff >= 0 ? "+" : "";
          subEl.textContent = sign + diff.toFixed(1) + "% vs last month's MOM";
          subEl.className = "rev-kpi-growth " + (diff >= 0 ? "is-up" : "is-down");
        } else {
          subEl.textContent = "";
          subEl.className = "rev-kpi-growth";
        }
      }
    }
  }

  /* ── Line chart (mirrors dashboard) ────── */
  const CHART_W = 900;
  const CHART_H = 240;
  const PLOT_LEFT = 72;
  const PLOT_RIGHT = 860;
  const PLOT_TOP = 20;
  const PLOT_BOTTOM = 178;
  const PLOT_W = PLOT_RIGHT - PLOT_LEFT;
  const PLOT_H = PLOT_BOTTOM - PLOT_TOP;

  function niceAxisMax(value) {
    if (value <= 0) return 100;
    var magnitude = Math.pow(10, Math.floor(Math.log10(value)));
    var normalized = value / magnitude;
    var nice;
    if (normalized <= 1) nice = 1;
    else if (normalized <= 2) nice = 2;
    else if (normalized <= 5) nice = 5;
    else nice = 10;
    return nice * magnitude;
  }

  function svg(tag, attrs, children) {
    var el = document.createElementNS("http://www.w3.org/2000/svg", tag);
    Object.keys(attrs).forEach(function (k) { el.setAttribute(k, attrs[k]); });
    if (children) el.innerHTML = children;
    return el;
  }

  function renderLineChart(bars, barsMax, rangeLabelX) {
    var chartSvg = document.getElementById("rev-line-chart-svg");
    var titleEl = document.getElementById("rev-chart-title");
    if (!chartSvg) return;

    if (titleEl) titleEl.textContent = "Revenue";

    chartSvg.setAttribute("viewBox", "0 0 " + CHART_W + " " + CHART_H);
    while (chartSvg.firstChild) chartSvg.removeChild(chartSvg.firstChild);

    var count = bars.length;
    if (count === 0) {
      chartSvg.appendChild(svg("text", { x: CHART_W / 2, y: CHART_H / 2, "text-anchor": "middle", "font-size": "13", fill: "#8098b0" }, "No data for this range"));
      return;
    }

    var axisMax = niceAxisMax(barsMax > 0 ? barsMax : 1);

    // Build chart points
    var pointCount = Math.max(1, count - 1);
    var points = bars.map(function (bar, i) {
      var x = PLOT_LEFT + (PLOT_W * i / pointCount);
      var y = PLOT_BOTTOM - (bar.amount / axisMax) * PLOT_H;
      return { x: Math.round(x * 100) / 100, y: Math.round(y * 100) / 100, label: bar.label, amount: bar.amount };
    });

    // Vertical grid lines
    points.forEach(function (p) {
      chartSvg.appendChild(svg("line", { x1: p.x, y1: PLOT_TOP, x2: p.x, y2: PLOT_BOTTOM, class: "reporting-grid-x" }));
    });

    // Horizontal grid lines + Y tick labels
    var Y_TICKS = 5;
    for (var i = 0; i < Y_TICKS; i++) {
      var ratio = i / (Y_TICKS - 1);
      var tickY = Math.round((PLOT_BOTTOM - PLOT_H * ratio) * 100) / 100;
      var tickVal = Math.round(axisMax * ratio);
      chartSvg.appendChild(svg("line", { x1: PLOT_LEFT, y1: tickY, x2: PLOT_RIGHT, y2: tickY, class: "reporting-grid-y" }));
      chartSvg.appendChild(svg("text", { x: PLOT_LEFT - 8, y: tickY + 3.5, "text-anchor": "end", class: "reporting-axis-label-y" }, "$" + tickVal.toLocaleString("en-US")));
    }

    // Axis lines
    chartSvg.appendChild(svg("line", { x1: PLOT_LEFT, y1: PLOT_BOTTOM, x2: PLOT_RIGHT, y2: PLOT_BOTTOM, class: "reporting-axis" }));
    chartSvg.appendChild(svg("line", { x1: PLOT_LEFT, y1: PLOT_TOP, x2: PLOT_LEFT, y2: PLOT_BOTTOM, class: "reporting-axis" }));

    // Polyline
    if (count > 1) {
      var polyPts = points.map(function (p) { return p.x + "," + p.y; }).join(" ");
      chartSvg.appendChild(svg("polyline", { points: polyPts, class: "reporting-line" }));
    }

    // Data points
    points.forEach(function (p) {
      chartSvg.appendChild(svg("circle", { cx: p.x, cy: p.y, r: "3.3", class: "reporting-point" }));
    });

    // X day labels — thin out if too many
    var labelEvery = count <= 14 ? 1 : count <= 30 ? 3 : 7;
    points.forEach(function (p, i) {
      if (i % labelEvery === 0 || i === count - 1) {
        var parts = p.label.split(" ");
        var shortLabel = count > 14 ? (parts[1] || p.label) : p.label;
        chartSvg.appendChild(svg("text", { x: p.x, y: CHART_H - 22, "text-anchor": "middle", class: "reporting-axis-label-x reporting-day-label" }, shortLabel));
      }
    });

    // X axis title — chosen date range
    var xCenter = (PLOT_LEFT + PLOT_RIGHT) / 2;
    chartSvg.appendChild(svg("text", { x: xCenter, y: CHART_H - 6, "text-anchor": "middle", class: "reporting-axis-label-x reporting-axis-title-x" }, rangeLabelX));

    // Y axis title
    var yCenter = (PLOT_TOP + PLOT_BOTTOM) / 2;
    var yTitleEl = svg("text", { x: 18, y: yCenter, transform: "rotate(-90 18 " + yCenter + ")", "text-anchor": "middle", class: "reporting-axis-label-y reporting-axis-title-y" }, "Total Revenue");
    chartSvg.appendChild(yTitleEl);
  }

  /* ── Service type bars ──────────────────── */
  function renderServiceBars(types) {
    if (!serviceBarsEl) return;

    if (!types || types.length === 0) {
      serviceBarsEl.innerHTML = '<div class="rev-placeholder">No service data for this range.</div>';
      return;
    }

    const rows = types.map(function (t) {
      return (
        '<div class="rev-service-bar-row">' +
          '<span class="rev-service-bar-label" title="' + t.service_type + '">' + t.service_type + '</span>' +
          '<div class="rev-service-bar-track">' +
            '<div class="rev-service-bar-fill" style="width:' + t.pct_of_max + '%"></div>' +
          '</div>' +
          '<span class="rev-service-bar-amount">' + formatCurrency(t.amount) + '</span>' +
        '</div>'
      );
    });

    serviceBarsEl.innerHTML = rows.join("");
  }

  function renderEquipmentBars(types) {
    if (!equipmentBarsEl) return;

    if (!types || types.length === 0) {
      equipmentBarsEl.innerHTML = '<div class="rev-placeholder">No equipment data for this range.</div>';
      return;
    }

    const rows = types.map(function (t) {
      return (
        '<div class="rev-service-bar-row">' +
          '<span class="rev-service-bar-label" title="' + t.equipment_type + '">' + t.equipment_type + '</span>' +
          '<div class="rev-service-bar-track">' +
            '<div class="rev-service-bar-fill" style="width:' + t.pct_of_max + '%"></div>' +
          '</div>' +
          '<span class="rev-service-bar-amount">' + formatCurrency(t.amount) + '</span>' +
        '</div>'
      );
    });

    equipmentBarsEl.innerHTML = rows.join("");
  }

  function renderEmployeeDonut(splits) {
    if (!employeeDonutEl || !employeeLegendEl) return;

    if (!splits || splits.length === 0) {
      employeeDonutEl.innerHTML = "";
      employeeLegendEl.innerHTML = '<div class="rev-placeholder">No employee data for this range.</div>';
      return;
    }

    const colors = ["#4f8ef7", "#63c08a", "#f6a737", "#9b6edc", "#dd6b7a", "#5bb6c8"];
    const radius = 46;
    const circumference = 2 * Math.PI * radius;
    let offset = 0;
    let svgHtml = "";

    splits.forEach(function (item, index) {
      const pct = Number(item.pct) || 0;
      const segment = (pct / 100) * circumference;
      const color = colors[index % colors.length];
      svgHtml +=
        '<circle r="' + radius + '" cx="60" cy="60" fill="none" stroke="' + color + '" stroke-width="20" ' +
        'stroke-dasharray="' + segment.toFixed(2) + ' ' + circumference.toFixed(2) + '" ' +
        'stroke-dashoffset="-' + offset.toFixed(2) + '" transform="rotate(-90 60 60)"></circle>';
      offset += segment;
    });

    employeeDonutEl.innerHTML = svgHtml;

    const legendRows = splits.map(function (item, index) {
      const color = colors[index % colors.length];
      return (
        '<div class="rev-donut-legend-item">' +
          '<span class="rev-donut-swatch" style="background:' + color + '"></span>' +
          '<span>' + item.employee + '</span>' +
          '<span class="rev-donut-pct">' + formatCurrency(item.amount) + ' | ' + item.pct.toFixed(1) + '%</span>' +
        '</div>'
      );
    });

    employeeLegendEl.innerHTML = legendRows.join("");
  }

  /* ── Fetch & render ─────────────────────── */
  function fetchAndRender() {
    const start = startInput.value;
    const end = endInput.value;
    if (!start || !end) return;

    const url = DATA_URL + "?start_date=" + encodeURIComponent(start) + "&end_date=" + encodeURIComponent(end);

    fetch(url, { credentials: "same-origin" })
      .then(function (resp) { return resp.json(); })
      .then(function (data) {
        if (data.error) {
          console.error("Revenue report error:", data.error);
          return;
        }

        setKpi("rev-total-revenue", "rev-total-revenue-growth", data.total_revenue, data.total_revenue_yoy_pct, true);
        setKpi("rev-avg-job-value", "rev-avg-job-value-growth", data.avg_job_value, data.avg_job_value_yoy_pct, true);
        setKpi("rev-active-contracts", "rev-active-contracts-growth", data.active_contracts, data.active_contracts_yoy_pct, false);
        setMomKpi(data.mom_pct, data.prev_mom_pct);

        var rangeLabel = buildRangeLabel(start, end);
        renderLineChart(data.bars, data.bars_max, rangeLabel);
        renderServiceBars(data.revenue_by_service_type);
        renderEquipmentBars(data.revenue_by_equipment_type);
        renderEmployeeDonut(data.revenue_by_employee);
      })
      .catch(function (err) {
        console.error("Revenue report fetch failed:", err);
      });
  }

  /* ── Event wiring ───────────────────────── */
  timeframeBtns.forEach(function (btn) {
    btn.addEventListener("click", function () {
      const days = parseInt(btn.getAttribute("data-days"), 10);
      setDateRange(days);
      setActiveBtn(btn);
      fetchAndRender();
    });
  });

  startInput.addEventListener("change", function () {
    setActiveBtn(null);
    fetchAndRender();
  });

  endInput.addEventListener("change", function () {
    setActiveBtn(null);
    fetchAndRender();
  });

  /* ── Init ───────────────────────────────── */
  setDateRange(7);
  fetchAndRender();
}());
