(function () {
  function parseRules(form) {
    if (!form) {
      return [];
    }
    const raw = form.getAttribute("data-markup-rules") || "[]";
    try {
      const parsed = JSON.parse(raw);
      if (!Array.isArray(parsed)) {
        return [];
      }
      return parsed
        .map((rule) => ({
          range_min: Number(rule.range_min),
          range_max: rule.range_max === null || rule.range_max === "" ? null : Number(rule.range_max),
          markup_percent: Number(rule.markup_percent),
        }))
        .filter((rule) => !Number.isNaN(rule.range_min) && (rule.range_max === null || !Number.isNaN(rule.range_max)) && !Number.isNaN(rule.markup_percent))
        .sort((a, b) => a.range_min - b.range_min);
    } catch (error) {
      return [];
    }
  }

  function resolveRule(cost, rules) {
    for (let index = 0; index < rules.length; index += 1) {
      const rule = rules[index];
      if (cost < rule.range_min) {
        continue;
      }
      if (rule.range_max === null || cost <= rule.range_max) {
        return rule;
      }
    }
    return null;
  }

  function formatRange(rule) {
    const minText = "$" + Number(rule.range_min).toFixed(2);
    if (rule.range_max === null) {
      return minText + " - no limit";
    }
    return minText + " - $" + Number(rule.range_max).toFixed(2);
  }

  function formatSellMessage(context, sellValue, rule, unitLabelValue) {
    const sellText = "$" + Number(sellValue).toFixed(2);
    if (context === "material") {
      const suffix = unitLabelValue ? " " + unitLabelValue : " per unit";
      return "Auto-populated from rule " + formatRange(rule) + " at " + Number(rule.markup_percent).toFixed(2) + "%: " + sellText + suffix;
    }
    return "Auto-populated from rule " + formatRange(rule) + " at " + Number(rule.markup_percent).toFixed(2) + "%: " + sellText;
  }

  function initMarkupAutofill(formId) {
    const form = document.getElementById(formId);
    if (!form) {
      return;
    }

    const context = String(form.getAttribute("data-markup-context") || "part").toLowerCase();
    const rules = parseRules(form);
    const costInput = form.querySelector(context === "material" ? "#material-cost-price" : "#cost-price");
    const sellInput = form.querySelector(context === "material" ? "#material-sell-price" : "#sell-price");
    const unitLabelInput = form.querySelector("#material-unit-label");
    const autoField = form.querySelector('input[name="sell_price_auto_populated"]');
    const messageNode = form.querySelector("#markup-autofill-message");

    if (!costInput || !sellInput || !autoField) {
      return;
    }

    let manualOverride = autoField.value !== "true" && String(sellInput.value || "").trim() !== "";

    function setMessage(text) {
      if (!messageNode) {
        return;
      }
      messageNode.textContent = text || "";
    }

    function applyAutofill() {
      const costText = String(costInput.value || "").trim();
      if (!costText) {
        return;
      }
      const cost = Number(costText);
      if (Number.isNaN(cost) || cost < 0) {
        return;
      }

      if (!rules.length) {
        setMessage("");
        return;
      }

      if (manualOverride) {
        return;
      }

      const rule = resolveRule(cost, rules);
      if (!rule) {
        setMessage("No markup rule matched this cost. Enter sell price manually.");
        autoField.value = "false";
        return;
      }

      const sell = (cost * (1 + (rule.markup_percent / 100))).toFixed(2);
      sellInput.value = sell;
      autoField.value = "true";
      const unitLabelValue = unitLabelInput ? String(unitLabelInput.value || "").trim() : "";
      setMessage(formatSellMessage(context, sell, rule, unitLabelValue));
    }

    sellInput.addEventListener("input", function () {
      manualOverride = true;
      autoField.value = "false";
      setMessage("");
    });

    costInput.addEventListener("blur", applyAutofill);
    costInput.addEventListener("change", applyAutofill);

    if (unitLabelInput) {
      unitLabelInput.addEventListener("input", function () {
        if (autoField.value === "true") {
          applyAutofill();
        }
      });
    }

    if (autoField.value === "true") {
      manualOverride = false;
      applyAutofill();
    }
  }

  initMarkupAutofill("create-part-form");
  initMarkupAutofill("create-service-form");
})();
