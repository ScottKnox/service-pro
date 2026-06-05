(function () {
  const form = document.getElementById("create-job-form");
  if (!form) {
    return;
  }

  const servicesList = document.getElementById("job-services-list");
  const serviceOptionsTemplate = document.getElementById("service-options-template");
  const standaloneList = document.getElementById("job-standalone-items-list");
  const standaloneToggle = document.getElementById("standalone-items-toggle");
  const addStandalonePartButton = document.getElementById("add-standalone-part-button");
  const addStandaloneMaterialButton = document.getElementById("add-standalone-material-button");
  const addStandaloneEquipmentButton = document.getElementById("add-standalone-equipment-button");
  const addServiceButton = document.getElementById("add-job-service-button");

  const servicesCatalog = form.dataset.servicesCatalog ? JSON.parse(form.dataset.servicesCatalog) : {};
  const allServices = form.dataset.services ? JSON.parse(form.dataset.services) : [];
  const allParts = form.dataset.parts ? JSON.parse(form.dataset.parts) : [];
  const allMaterials = form.dataset.materials ? JSON.parse(form.dataset.materials) : [];
  const allEquipments = form.dataset.equipments ? JSON.parse(form.dataset.equipments) : [];
  const existingParts = form.dataset.existingParts ? JSON.parse(form.dataset.existingParts) : [];
  const existingMaterials = form.dataset.existingMaterials ? JSON.parse(form.dataset.existingMaterials) : [];
  const existingEquipments = form.dataset.existingEquipments ? JSON.parse(form.dataset.existingEquipments) : [];
  const partCategories = form.dataset.partCategories ? JSON.parse(form.dataset.partCategories) : [];
  const materialCategories = form.dataset.materialCategories ? JSON.parse(form.dataset.materialCategories) : [];
  const equipmentCategories = form.dataset.equipmentCategories ? JSON.parse(form.dataset.equipmentCategories) : [];

  const customerProperties = form.dataset.customerProperties ? JSON.parse(form.dataset.customerProperties) : [];
  const initialHvacSystems = form.dataset.initialHvacSystems ? JSON.parse(form.dataset.initialHvacSystems) : [];
  const customerId = form.dataset.customerId || "";

  const propertySelect = document.querySelector("[data-job-property-select]");
  const addressPreview = document.querySelector("[data-job-property-address]");
  const addressLine1Field = document.getElementById("job-address-line-1");
  const addressLine2Field = document.getElementById("job-address-line-2");
  const cityField = document.getElementById("job-city");
  const stateField = document.getElementById("job-state");
  const zipField = document.getElementById("job-zip-code");

  const submitButton = document.getElementById("submit-button");
  const dateField = document.getElementById("job-date") || document.getElementById("proposed-job-date");
  const timeField = document.getElementById("job-time") || document.getElementById("proposed-job-time");
  const clearDateTimeButton = document.getElementById("clear-date-time");
  const scheduleTypeField = document.getElementById("job-schedule-type");
  const recurringFrequencyField = document.getElementById("recurring-frequency");
  const recurringEndTypeField = document.getElementById("recurring-end-type");
  const recurringEndDateField = document.getElementById("recurring-end-date");
  const recurringEndAfterField = document.getElementById("recurring-end-after");
  const recurringSettings = document.getElementById("job-recurring-settings");
  const recurringEndDateWrap = document.getElementById("recurring-end-date-wrap");
  const recurringEndAfterWrap = document.getElementById("recurring-end-after-wrap");

  let currentHvacSystems = initialHvacSystems.slice();

  const servicesById = {};
  allServices.forEach(function (service) {
    const id = String(service._id || "").trim();
    if (id) {
      servicesById[id] = service;
    }
  });

  const categoryNameById = {
    part: {},
    material: {},
    equipment: {},
  };

  partCategories.forEach(function (category) {
    categoryNameById.part[String(category._id || "").trim()] = String(category.name || "").trim();
  });
  materialCategories.forEach(function (category) {
    categoryNameById.material[String(category._id || "").trim()] = String(category.name || "").trim();
  });
  equipmentCategories.forEach(function (category) {
    categoryNameById.equipment[String(category._id || "").trim()] = String(category.name || "").trim();
  });

  function normalizeText(value) {
    return String(value || "").trim().toLowerCase();
  }

  function isActiveItem(item) {
    return item && item.is_active !== false;
  }

  function formatAddress(property) {
    if (!property) {
      return "Select a property above to see the job address.";
    }
    const line1 = String(property.address_line_1 || "").trim();
    const line2 = String(property.address_line_2 || "").trim();
    const city = String(property.city || "").trim();
    const state = String(property.state || "").trim();
    const zip = String(property.zip_code || "").trim();
    const name = String(property.property_name || "Property").trim();
    const street = [line1, line2].filter(Boolean).join(", ");
    const cityLine = [city, state].filter(Boolean).join(", ");
    return name + "\n" + [street, [cityLine, zip].filter(Boolean).join(" ")].filter(Boolean).join("\n");
  }

  function applyProperty(propertyId) {
    const selected = customerProperties.find(function (property) {
      return String(property.property_id || "").trim() === String(propertyId || "").trim();
    });

    if (selected) {
      if (addressLine1Field) addressLine1Field.value = selected.address_line_1 || "";
      if (addressLine2Field) addressLine2Field.value = selected.address_line_2 || "";
      if (cityField) cityField.value = selected.city || "";
      if (stateField) stateField.value = selected.state || "";
      if (zipField) zipField.value = selected.zip_code || "";
      if (addressPreview) {
        addressPreview.textContent = formatAddress(selected);
      }
    }
  }

  function fetchHvacSystemsForProperty(propertyId) {
    const normalizedPropertyId = String(propertyId || "").trim();
    if (!normalizedPropertyId || !customerId) {
      currentHvacSystems = [];
      refreshAllHvacSelectors();
      return;
    }

    const url = "/api/hvac-systems-for-property?customer_id=" + encodeURIComponent(customerId) + "&property_id=" + encodeURIComponent(normalizedPropertyId);
    fetch(url, { credentials: "same-origin" })
      .then(function (response) {
        return response.json();
      })
      .then(function (payload) {
        currentHvacSystems = Array.isArray(payload.hvac_systems) ? payload.hvac_systems : [];
        refreshAllHvacSelectors();
      })
      .catch(function () {
        currentHvacSystems = [];
        refreshAllHvacSelectors();
      });
  }

  function buildHvacOptions(selectedId) {
    const normalizedSelectedId = String(selectedId || "").trim();
    const options = ['<option value="">-- No HVAC System --</option>'];
    currentHvacSystems.forEach(function (system) {
      const id = String(system.id || "").trim();
      if (!id) {
        return;
      }
      const title = String(system.title || system.system_type || "HVAC System").trim();
      options.push('<option value="' + id + '"' + (id === normalizedSelectedId ? " selected" : "") + ">" + title + "</option>");
    });
    return options.join("");
  }

  function refreshAllHvacSelectors() {
    form.querySelectorAll("[data-hvac-select]").forEach(function (select) {
      const currentValue = select.value;
      select.innerHTML = buildHvacOptions(currentValue);
    });
  }

  function formatCurrency(value) {
    const number = Number.parseFloat(String(value || "").replace(/[^0-9.-]/g, ""));
    if (Number.isNaN(number)) {
      return "$0.00";
    }
    return "$" + number.toFixed(2);
  }

  function parseCurrency(value) {
    const number = Number.parseFloat(String(value || "").replace(/[^0-9.-]/g, ""));
    return Number.isNaN(number) ? 0 : number;
  }

  function makeField(labelText, html) {
    return '<div class="add-customer-form-field">' +
      '<label>' + labelText + '</label>' +
      html +
      '</div>';
  }

  function getServiceRowIndex(row) {
    return String(row.dataset.serviceIndex || "").trim();
  }

  function updatePromptCounts(row) {
    const prompts = row.querySelectorAll("[data-contextual-prompt]");
    prompts.forEach(function (prompt) {
      const type = prompt.dataset.type;
      const categoryId = prompt.dataset.categoryId;
      const total = Number.parseInt(prompt.dataset.totalCount || "0", 10) || 0;
      const used = row.querySelectorAll('[data-sub-item][data-type="' + type + '"][data-category-id="' + categoryId + '"]').length;
      const remaining = Math.max(total - used, 0);
      const title = prompt.querySelector("summary .contextual-prompt-title");
      if (title) {
        title.textContent = "Add " + (prompt.dataset.categoryName || "item") + " (" + remaining + " available)";
      }
      prompt.querySelectorAll("[data-contextual-add]").forEach(function (button) {
        button.disabled = remaining <= 0;
      });
    });
  }

  function bindSubItemPricing(subItemRow, includedLabel) {
    const priceInput = subItemRow.querySelector("[data-sub-item-price-input]");
    const hiddenPriceInput = subItemRow.querySelector("[data-sub-item-price-hidden]");
    if (!priceInput || !hiddenPriceInput) {
      return;
    }
    const syncPrice = function () {
      const value = formatCurrency(priceInput.value);
      priceInput.value = value;
      hiddenPriceInput.value = value;
      if (includedLabel) {
        includedLabel.style.display = parseCurrency(value) === 0 ? "" : "none";
      }
    };
    priceInput.addEventListener("blur", syncPrice);
    syncPrice();
  }

  function createContextualSubItem(row, type, categoryId, item) {
    const serviceIndex = getServiceRowIndex(row);
    const serviceHvacSelect = row.querySelector('[name="service_hvac_system_id[]"]');
    const serviceHvacId = serviceHvacSelect ? serviceHvacSelect.value : "";

    const subItemsWrap = row.querySelector(".service-sub-items");
    if (!subItemsWrap) {
      return;
    }

    const itemName = type === "part"
      ? String(item.part_name || item.name || "Part").trim()
      : type === "material"
        ? String(item.material_name || item.name || "Material").trim()
        : String(item.equipment_name || item.name || "Equipment").trim();

    const subRow = document.createElement("div");
    subRow.className = "service-sub-item-row";
    subRow.classList.add("is-entering");
    subRow.dataset.subItem = "1";
    subRow.dataset.type = type;
    subRow.dataset.categoryId = categoryId;

    const label = document.createElement("span");
    label.className = "service-sub-item-name";
    label.textContent = itemName;

    const priceInput = document.createElement("input");
    priceInput.type = "text";
    priceInput.className = "service-sub-item-price";
    priceInput.value = "$0.00";
    priceInput.setAttribute("data-sub-item-price-input", "1");

    const includedLabel = document.createElement("span");
    includedLabel.className = "service-sub-item-included";
    includedLabel.textContent = "included";

    const removeButton = document.createElement("button");
    removeButton.type = "button";
    removeButton.className = "service-sub-item-remove";
    removeButton.textContent = "×";

    const controls = document.createElement("div");
    controls.className = "service-sub-item-controls";
    controls.appendChild(priceInput);
    controls.appendChild(includedLabel);
    controls.appendChild(removeButton);

    subRow.appendChild(label);
    subRow.appendChild(controls);

    const hiddenInputsWrap = document.createElement("div");
    hiddenInputsWrap.className = "service-sub-item-hidden-inputs";

    const hvacHidden = document.createElement("input");
    hvacHidden.type = "hidden";
    hvacHidden.name = type + "_hvac_system_id[]";
    hvacHidden.value = serviceHvacId;
    hvacHidden.setAttribute("data-sub-item-hvac-hidden", "1");
    hiddenInputsWrap.appendChild(hvacHidden);

    let priceHiddenName = "";

    if (type === "part") {
      const partCode = document.createElement("input");
      partCode.type = "hidden";
      partCode.name = "part_code[]";
      partCode.value = String(item._id || "").trim();
      hiddenInputsWrap.appendChild(partCode);

      priceHiddenName = "part_unit_cost[]";
    } else if (type === "material") {
      const materialName = document.createElement("input");
      materialName.type = "hidden";
      materialName.name = "material_name[]";
      materialName.value = String(item.material_name || item.name || "").trim();
      hiddenInputsWrap.appendChild(materialName);

      const quantity = document.createElement("input");
      quantity.type = "hidden";
      quantity.name = "material_quantity_used[]";
      quantity.value = "1";
      hiddenInputsWrap.appendChild(quantity);

      const unit = document.createElement("input");
      unit.type = "hidden";
      unit.name = "material_unit_of_measure[]";
      unit.value = String(item.unit_of_measure || "").trim();
      hiddenInputsWrap.appendChild(unit);

      priceHiddenName = "material_price[]";
    } else {
      const equipmentName = document.createElement("input");
      equipmentName.type = "hidden";
      equipmentName.name = "equipment_name[]";
      equipmentName.value = String(item.equipment_name || item.name || "").trim();
      hiddenInputsWrap.appendChild(equipmentName);

      const quantityInstalled = document.createElement("input");
      quantityInstalled.type = "hidden";
      quantityInstalled.name = "equipment_quantity_installed[]";
      quantityInstalled.value = "1";
      hiddenInputsWrap.appendChild(quantityInstalled);

      const serialWrap = document.createElement("div");
      serialWrap.className = "service-sub-item-serial-wrap";
      const serialLabel = document.createElement("label");
      serialLabel.textContent = "Serial Number";
      const serialVisible = document.createElement("input");
      serialVisible.type = "text";
      serialVisible.placeholder = "Optional";
      const serialHidden = document.createElement("input");
      serialHidden.type = "hidden";
      serialHidden.name = "equipment_serial_number[]";
      serialVisible.addEventListener("input", function () {
        serialHidden.value = serialVisible.value;
      });
      serialWrap.appendChild(serialLabel);
      serialWrap.appendChild(serialVisible);
      subRow.appendChild(serialWrap);
      hiddenInputsWrap.appendChild(serialHidden);

      priceHiddenName = "equipment_price[]";
    }

    const priceHidden = document.createElement("input");
    priceHidden.type = "hidden";
    priceHidden.name = priceHiddenName;
    priceHidden.value = "$0.00";
    priceHidden.setAttribute("data-sub-item-price-hidden", "1");
    hiddenInputsWrap.appendChild(priceHidden);

    subRow.appendChild(hiddenInputsWrap);

    bindSubItemPricing(subRow, includedLabel);

    removeButton.addEventListener("click", function () {
      subRow.remove();
      updatePromptCounts(row);
      validateForm();
    });

    subItemsWrap.appendChild(subRow);

    window.setTimeout(function () {
      subRow.classList.remove("is-entering");
    }, 260);

    row.querySelectorAll("details[data-contextual-prompt]").forEach(function (details) {
      details.open = false;
    });

    updatePromptCounts(row);
    validateForm();
  }

  function filterItemsByCategory(type, categoryId) {
    const categoryName = normalizeText(categoryNameById[type][categoryId]);
    if (!categoryName) {
      return [];
    }

    const source = type === "part" ? allParts : type === "material" ? allMaterials : allEquipments;
    return source.filter(function (item) {
      if (!isActiveItem(item)) {
        return false;
      }
      return normalizeText(item.category) === categoryName;
    });
  }

  function buildContextualPrompt(row, type, categoryId) {
    const categoryName = String(categoryNameById[type][categoryId] || "").trim();
    if (!categoryName) {
      return null;
    }

    const matchingItems = filterItemsByCategory(type, categoryId);
    if (!matchingItems.length) {
      return null;
    }

    const details = document.createElement("details");
    details.className = "contextual-prompt";
    details.dataset.contextualPrompt = "1";
    details.dataset.type = type;
    details.dataset.categoryId = categoryId;
    details.dataset.categoryName = categoryName;
    details.dataset.totalCount = String(matchingItems.length);

    const summary = document.createElement("summary");
    const title = document.createElement("span");
    title.className = "contextual-prompt-title";
    summary.appendChild(title);
    details.appendChild(summary);

    const list = document.createElement("div");
    list.className = "contextual-prompt-items";

    matchingItems.forEach(function (item) {
      const rowEl = document.createElement("div");
      rowEl.className = "contextual-prompt-item";

      const name = document.createElement("span");
      name.className = "contextual-prompt-item-name";
      name.textContent = type === "part"
        ? String(item.part_name || item.name || "Part").trim()
        : type === "material"
          ? String(item.material_name || item.name || "Material").trim()
          : String(item.equipment_name || item.name || "Equipment").trim();

      const addButton = document.createElement("button");
      addButton.type = "button";
      addButton.className = "contextual-prompt-item-add";
      addButton.setAttribute("data-contextual-add", "1");
      addButton.textContent = "Add";
      addButton.addEventListener("click", function () {
        createContextualSubItem(row, type, categoryId, item);
      });

      rowEl.appendChild(name);
      rowEl.appendChild(addButton);
      list.appendChild(rowEl);
    });

    details.appendChild(list);
    return details;
  }

  function rebuildContextualPrompts(row) {
    const serviceSelect = row.querySelector('[name="service_code[]"]');
    const promptWrap = row.querySelector(".service-contextual-prompts");
    if (!serviceSelect || !promptWrap) {
      return;
    }

    promptWrap.innerHTML = "";
    const selectedId = String(serviceSelect.value || "").trim();
    if (!selectedId) {
      return;
    }

    const service = servicesById[selectedId] || {};
    const partCategoryIds = Array.isArray(service.associated_part_category_ids) ? service.associated_part_category_ids : [];
    const materialCategoryIds = Array.isArray(service.associated_material_category_ids) ? service.associated_material_category_ids : [];
    const equipmentCategoryIds = Array.isArray(service.associated_equipment_category_ids) ? service.associated_equipment_category_ids : [];

    const sequences = [
      { type: "part", ids: partCategoryIds },
      { type: "material", ids: materialCategoryIds },
      { type: "equipment", ids: equipmentCategoryIds },
    ];

    sequences.forEach(function (sequence) {
      sequence.ids.forEach(function (rawCategoryId) {
        const categoryId = String(rawCategoryId || "").trim();
        if (!categoryId) {
          return;
        }
        const prompt = buildContextualPrompt(row, sequence.type, categoryId);
        if (prompt) {
          promptWrap.appendChild(prompt);
        }
      });
    });

    updatePromptCounts(row);
  }

  function bindServiceRow(row) {
    const serviceIndex = getServiceRowIndex(row);
    const serviceSelect = row.querySelector('[name="service_code[]"]');
    const priceInput = row.querySelector('[name="service_price[]"]');
    const hoursInput = row.querySelector('[name="service_hours[]"]');
    const emergencyToggle = row.querySelector('[data-service-emergency-toggle]');
    const emergencyHidden = row.querySelector('[name="service_emergency_call[]"]');

    const removeButton = row.querySelector(".job-row-remove-button");
    if (removeButton) {
      removeButton.addEventListener("click", function () {
        const nonZeroSubItems = row.querySelectorAll("[data-sub-item-price-hidden]");
        let hasNonZero = false;
        nonZeroSubItems.forEach(function (input) {
          if (parseCurrency(input.value) > 0) {
            hasNonZero = true;
          }
        });

        if (hasNonZero && !window.confirm("This service has sub line items with non-zero sell price. Remove service and all sub line items?")) {
          return;
        }

        row.remove();
        ensureAtLeastOneServiceRow();
        validateForm();
      });
    }

    if (serviceSelect) {
      serviceSelect.addEventListener("change", function () {
        const selectedService = servicesCatalog[serviceSelect.value] || {};
        if (priceInput) {
          priceInput.value = selectedService.standard_price || selectedService.price || "$0.00";
        }
        if (hoursInput) {
          hoursInput.value = selectedService.estimated_hours || "";
        }
        if (emergencyToggle && emergencyHidden) {
          emergencyToggle.checked = false;
          emergencyHidden.value = "no";
        }
        rebuildContextualPrompts(row);
        validateForm();
      });
    }

    if (emergencyToggle && emergencyHidden) {
      emergencyToggle.addEventListener("change", function () {
        emergencyHidden.value = emergencyToggle.checked ? "yes" : "no";
        const selectedService = servicesCatalog[serviceSelect.value] || {};
        if (priceInput) {
          if (emergencyToggle.checked) {
            priceInput.value = selectedService.emergency_price || selectedService.standard_price || selectedService.price || "$0.00";
          } else {
            priceInput.value = selectedService.standard_price || selectedService.price || "$0.00";
          }
        }
      });
    }

    const hvacSelect = row.querySelector('[name="service_hvac_system_id[]"]');
    if (hvacSelect) {
      hvacSelect.innerHTML = buildHvacOptions(hvacSelect.value);
      hvacSelect.addEventListener("change", function () {
        const selectedHvacId = String(hvacSelect.value || "").trim();
        row.querySelectorAll("[data-sub-item-hvac-hidden]").forEach(function (input) {
          input.value = selectedHvacId;
        });
      });
    }

    rebuildContextualPrompts(row);
  }

  function ensureServiceRowLayout(row) {
    if (!row) {
      return;
    }

    let promptWrap = row.querySelector(".service-contextual-prompts");
    if (!promptWrap) {
      promptWrap = document.createElement("div");
      promptWrap.className = "service-contextual-prompts";
      row.appendChild(promptWrap);
    }

    let subItems = row.querySelector(".service-sub-items");
    if (!subItems) {
      subItems = document.createElement("div");
      subItems.className = "service-sub-items";
      row.appendChild(subItems);
    }

    let hvacField = row.querySelector('[name="service_hvac_system_id[]"]');
    if (!hvacField) {
      const hvacWrap = document.createElement("div");
      hvacWrap.className = "add-customer-form-field";
      hvacWrap.innerHTML = '<label>Tag To System</label><select data-hvac-select="1" name="service_hvac_system_id[]">' + buildHvacOptions("") + '</select>';
      row.appendChild(hvacWrap);
      hvacField = hvacWrap.querySelector('[name="service_hvac_system_id[]"]');
    }

    let removeWrap = row.querySelector(".job-row-remove-wrap");
    if (!removeWrap) {
      removeWrap = document.createElement("div");
      removeWrap.className = "job-row-remove-wrap";
      removeWrap.innerHTML = '<button type="button" class="job-row-remove-button">Remove Service</button>';
      row.appendChild(removeWrap);
    }

    const hvacContainer = hvacField.closest(".add-customer-form-field") || hvacField;
    row.insertBefore(promptWrap, hvacContainer);
    row.insertBefore(subItems, hvacContainer);
    row.appendChild(removeWrap);
  }

  function createServiceRow(index) {
    const row = document.createElement("div");
    row.className = "add-customer-form-row job-service-row job-service-context-row";
    row.dataset.serviceIndex = String(index);

    row.innerHTML =
      makeField("Service", '<select id="service-code-' + index + '" name="service_code[]"><option value="">-- Select a service --</option>' + serviceOptionsTemplate.innerHTML + "</select>") +
      makeField("Price", '<input id="service-price-' + index + '" name="service_price[]" type="text" value="$0.00" />') +
      makeField("Hours", '<input id="service-hours-' + index + '" name="service_hours[]" type="number" step="0.25" min="0" placeholder="0" />') +
      makeField("Emergency Call?", '<input type="hidden" name="service_emergency_call[]" value="no" /><input data-service-emergency-toggle="1" type="checkbox" />') +
      '<div class="service-contextual-prompts"></div>' +
      '<div class="service-sub-items"></div>' +
      makeField("Tag To System", '<select data-hvac-select="1" name="service_hvac_system_id[]">' + buildHvacOptions("") + "</select>") +
      '<div class="job-row-remove-wrap"><button type="button" class="job-row-remove-button">Remove Service</button></div>';

    ensureServiceRowLayout(row);
    bindServiceRow(row);
    return row;
  }

  function ensureAtLeastOneServiceRow() {
    if (!servicesList) {
      return;
    }
    const rows = servicesList.querySelectorAll(".job-service-row");
    if (rows.length === 0) {
      servicesList.appendChild(createServiceRow(1));
    }
  }

  function addStandaloneRow(type, prefill) {
    if (!standaloneList) {
      return;
    }

    const row = document.createElement("div");
    row.className = "add-customer-form-row standalone-item-row";

    const prefillHvacId = prefill ? String(prefill.hvac_system_id || "").trim() : "";
    const hvacSelectHtml = '<select data-hvac-select="1" name="' + type + '_hvac_system_id[]">' + buildHvacOptions(prefillHvacId) + "</select>";

    if (type === "part") {
      const options = ['<option value="">-- Select part --</option>'];
      allParts.filter(isActiveItem).forEach(function (part) {
        options.push('<option value="' + String(part._id || "") + '">' + String(part.part_name || part.name || "Part") + "</option>");
      });
      row.innerHTML =
        makeField("Part", '<select name="part_code[]">' + options.join("") + "</select>") +
        makeField("Sell Price", '<input name="part_unit_cost[]" type="text" value="$0.00" />') +
        makeField("Tag To System", hvacSelectHtml) +
        '<div class="job-row-remove-wrap"><button type="button" class="job-row-remove-button">Remove</button></div>';

      const select = row.querySelector('[name="part_code[]"]');
      const price = row.querySelector('[name="part_unit_cost[]"]');
      if (prefill) {
        const prefillPartId = String(prefill.part_id || prefill.code || "").trim();
        const prefillPrice = String(prefill.price || prefill.unit_cost || "").trim();
        if (prefillPartId) {
          select.value = prefillPartId;
        }
        if (prefillPrice) {
          price.value = prefillPrice;
        }
      }
      select.addEventListener("change", function () {
        const selected = allParts.find(function (part) { return String(part._id || "") === select.value; }) || {};
        price.value = selected.sell_price || selected.unit_cost || "$0.00";
      });
    } else if (type === "material") {
      const options = ['<option value="">-- Select material --</option>'];
      allMaterials.filter(isActiveItem).forEach(function (material) {
        options.push('<option value="' + String(material.material_name || material.name || "") + '">' + String(material.material_name || material.name || "Material") + "</option>");
      });
      row.innerHTML =
        makeField("Material", '<select name="material_name[]">' + options.join("") + "</select>") +
        makeField("Quantity", '<input name="material_quantity_used[]" type="number" step="0.25" min="0" value="1" />') +
        makeField("Unit", '<input name="material_unit_of_measure[]" type="text" />') +
        makeField("Sell Price", '<input name="material_price[]" type="text" value="$0.00" />') +
        makeField("Tag To System", hvacSelectHtml) +
        '<div class="job-row-remove-wrap"><button type="button" class="job-row-remove-button">Remove</button></div>';

      const select = row.querySelector('[name="material_name[]"]');
      const unit = row.querySelector('[name="material_unit_of_measure[]"]');
      const quantity = row.querySelector('[name="material_quantity_used[]"]');
      const price = row.querySelector('[name="material_price[]"]');
      if (prefill) {
        const prefillMaterialName = String(prefill.material_name || prefill.name || "").trim();
        const prefillQuantity = String(prefill.quantity_used || prefill.quantity || "").trim();
        const prefillUnit = String(prefill.unit_of_measure || "").trim();
        const prefillPrice = String(prefill.price || "").trim();
        if (prefillMaterialName) {
          select.value = prefillMaterialName;
        }
        if (prefillQuantity) {
          quantity.value = prefillQuantity;
        }
        if (prefillUnit) {
          unit.value = prefillUnit;
        }
        if (prefillPrice) {
          price.value = prefillPrice;
        }
      }
      select.addEventListener("change", function () {
        const selected = allMaterials.find(function (material) { return String(material.material_name || material.name || "") === select.value; }) || {};
        unit.value = selected.unit_of_measure || "";
        price.value = selected.sell_price_per_unit || selected.price || "$0.00";
      });
    } else {
      const options = ['<option value="">-- Select equipment --</option>'];
      allEquipments.filter(isActiveItem).forEach(function (equipment) {
        const name = String(equipment.equipment_name || equipment.name || "Equipment");
        options.push('<option value="' + name + '">' + name + "</option>");
      });
      row.innerHTML =
        makeField("Equipment", '<select name="equipment_name[]">' + options.join("") + "</select>") +
        makeField("Quantity", '<input name="equipment_quantity_installed[]" type="number" step="0.25" min="0" value="1" />') +
        makeField("Sell Price", '<input name="equipment_price[]" type="text" value="$0.00" />') +
        makeField("Serial Number", '<input name="equipment_serial_number[]" type="text" placeholder="Optional" />') +
        makeField("Tag To System", hvacSelectHtml) +
        '<div class="job-row-remove-wrap"><button type="button" class="job-row-remove-button">Remove</button></div>';

      const select = row.querySelector('[name="equipment_name[]"]');
      const quantity = row.querySelector('[name="equipment_quantity_installed[]"]');
      const price = row.querySelector('[name="equipment_price[]"]');
      const serialNumber = row.querySelector('[name="equipment_serial_number[]"]');
      if (prefill) {
        const prefillEquipmentName = String(prefill.equipment_name || prefill.name || "").trim();
        const prefillQuantity = String(prefill.quantity_installed || prefill.quantity || "").trim();
        const prefillPrice = String(prefill.price || "").trim();
        const prefillSerial = String(prefill.serial_number || "").trim();
        if (prefillEquipmentName) {
          select.value = prefillEquipmentName;
        }
        if (prefillQuantity) {
          quantity.value = prefillQuantity;
        }
        if (prefillPrice) {
          price.value = prefillPrice;
        }
        if (prefillSerial) {
          serialNumber.value = prefillSerial;
        }
      }
      select.addEventListener("change", function () {
        const selected = allEquipments.find(function (equipment) {
          return String(equipment.equipment_name || equipment.name || "") === select.value;
        }) || {};
        price.value = selected.sell_price || "$0.00";
      });
    }

    const removeButton = row.querySelector(".job-row-remove-button");
    removeButton.addEventListener("click", function () {
      row.remove();
      validateForm();
    });

    standaloneList.appendChild(row);
    validateForm();
  }

  function preloadExistingStandaloneItems() {
    if (!standaloneList) {
      return;
    }

    (existingParts || []).forEach(function (part) {
      addStandaloneRow("part", part);
    });
    (existingMaterials || []).forEach(function (material) {
      addStandaloneRow("material", material);
    });
    (existingEquipments || []).forEach(function (equipment) {
      addStandaloneRow("equipment", equipment);
    });

    if (standaloneToggle && standaloneList.children.length > 0) {
      standaloneToggle.open = true;
    }
  }

  function syncRecurringUI() {
    const isRecurring = scheduleTypeField && scheduleTypeField.value === "recurring";
    if (recurringSettings) {
      recurringSettings.hidden = !isRecurring;
    }
    if (recurringEndDateWrap && recurringEndTypeField) {
      recurringEndDateWrap.hidden = !(isRecurring && recurringEndTypeField.value === "on_date");
    }
    if (recurringEndAfterWrap && recurringEndTypeField) {
      recurringEndAfterWrap.hidden = !(isRecurring && recurringEndTypeField.value === "after_occurrences");
    }
  }

  function validateForm() {
    const serviceSelects = servicesList ? servicesList.querySelectorAll('[name="service_code[]"]') : [];
    const hasService = Array.from(serviceSelects).some(function (select) {
      return String(select.value || "").trim() !== "";
    });
    const servicesError = document.getElementById("error-service-code") || document.getElementById("error-services");
    if (servicesError) {
      servicesError.style.display = hasService ? "none" : "block";
    }

    let valid = hasService;
    if (scheduleTypeField && scheduleTypeField.value === "recurring") {
      if (recurringFrequencyField && !recurringFrequencyField.value) {
        valid = false;
      }
      if (recurringEndTypeField && !recurringEndTypeField.value) {
        valid = false;
      }
      if (recurringEndTypeField && recurringEndTypeField.value === "on_date" && recurringEndDateField && !recurringEndDateField.value) {
        valid = false;
      }
      if (recurringEndTypeField && recurringEndTypeField.value === "after_occurrences" && recurringEndAfterField && !recurringEndAfterField.value) {
        valid = false;
      }
    }

    if (submitButton) {
      submitButton.disabled = !valid;
    }
    return valid;
  }

  if (propertySelect) {
    applyProperty(propertySelect.value);
    propertySelect.addEventListener("change", function () {
      applyProperty(propertySelect.value);
      fetchHvacSystemsForProperty(propertySelect.value);
      validateForm();
    });
  }

  if (addServiceButton) {
    addServiceButton.addEventListener("click", function () {
      const nextIndex = servicesList.querySelectorAll(".job-service-row").length + 1;
      servicesList.appendChild(createServiceRow(nextIndex));
      validateForm();
    });
  }

  if (addStandalonePartButton) {
    addStandalonePartButton.addEventListener("click", function () {
      if (standaloneToggle) {
        standaloneToggle.open = true;
      }
      addStandaloneRow("part");
    });
  }

  if (addStandaloneMaterialButton) {
    addStandaloneMaterialButton.addEventListener("click", function () {
      if (standaloneToggle) {
        standaloneToggle.open = true;
      }
      addStandaloneRow("material");
    });
  }

  if (addStandaloneEquipmentButton) {
    addStandaloneEquipmentButton.addEventListener("click", function () {
      if (standaloneToggle) {
        standaloneToggle.open = true;
      }
      addStandaloneRow("equipment");
    });
  }

  if (clearDateTimeButton && dateField && timeField) {
    clearDateTimeButton.addEventListener("click", function () {
      dateField.value = "";
      timeField.value = "";
    });
  }

  if (scheduleTypeField) {
    scheduleTypeField.addEventListener("change", function () {
      syncRecurringUI();
      validateForm();
    });
  }

  if (recurringEndTypeField) {
    recurringEndTypeField.addEventListener("change", function () {
      syncRecurringUI();
      validateForm();
    });
  }

  form.addEventListener("submit", function (event) {
    if (!validateForm()) {
      event.preventDefault();
    }
  });

  if (servicesList.querySelectorAll(".job-service-row").length === 0) {
    servicesList.appendChild(createServiceRow(1));
  } else {
    servicesList.querySelectorAll(".job-service-row").forEach(function (row) {
      ensureServiceRowLayout(row);
      if (!row.querySelector('[data-service-emergency-toggle]')) {
        const checkbox = row.querySelector('.service-emergency-call-toggle');
        if (checkbox) {
          checkbox.setAttribute('data-service-emergency-toggle', '1');
        }
      }
      bindServiceRow(row);
    });
  }

  preloadExistingStandaloneItems();

  syncRecurringUI();
  validateForm();
})();