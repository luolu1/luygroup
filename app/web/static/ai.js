(() => {
  "use strict";

  function setupProviderForm(form) {
    const preset = form.querySelector("select[data-preset]");
    // 注意：供应商模板的 <option> 也带有 data-base-url，这里必须限定到输入框。
    const baseUrl = form.querySelector("input[data-base-url]");
    const apiKey = form.querySelector("input[data-api-key]");
    const model = form.querySelector("input[data-model]");
    const modelSelect = form.querySelector("select[data-model-select]");
    const status = form.querySelector("[data-model-status]");
    const hint = form.querySelector("[data-preset-hint]");
    const providerId = form.dataset.providerId || "";
    if (!preset || !baseUrl || !model || !modelSelect) return;

    function populate(models, selectFirst) {
      modelSelect.innerHTML = "";
      const clean = Array.from(new Set((models || []).filter(Boolean)));
      if (!clean.length) {
        modelSelect.style.display = "none";
        return;
      }
      const placeholder = document.createElement("option");
      placeholder.value = "";
      placeholder.textContent = "从供应商模型列表选择…";
      modelSelect.appendChild(placeholder);
      clean.forEach((name) => {
        const option = document.createElement("option");
        option.value = name;
        option.textContent = name;
        modelSelect.appendChild(option);
      });
      modelSelect.style.display = "";
      if (selectFirst && !model.value.trim()) {
        model.value = clean[0];
        modelSelect.value = clean[0];
      }
    }

    function syncPresetFromUrl() {
      const current = (baseUrl.value || "").replace(/\/+$/, "");
      if (!current) return;
      for (const option of preset.options) {
        const candidate = (option.dataset.baseUrl || "").replace(/\/+$/, "");
        if (candidate && candidate === current) {
          preset.value = option.value;
          return;
        }
      }
      preset.value = "custom";
    }

    function applyPreset(forceBase) {
      const option = preset.selectedOptions[0];
      if (!option) return;
      const url = option.dataset.baseUrl || "";
      if (url && (forceBase || !baseUrl.value.trim())) baseUrl.value = url;
      const models = (option.dataset.models || "").split("|").filter(Boolean);
      if (models.length) populate(models, true);
      if (hint) hint.textContent = option.dataset.hint || "选择供应商后会自动填入接口地址。";
    }

    modelSelect.addEventListener("change", () => {
      if (modelSelect.value) model.value = modelSelect.value;
    });
    preset.addEventListener("change", () => applyPreset(true));

    const fetchButton = form.querySelector("[data-fetch-models]");
    if (fetchButton) {
      fetchButton.addEventListener("click", async () => {
        const url = (baseUrl.value || "").trim();
        if (!url) {
          if (status) status.textContent = "请先填写接口地址。";
          return;
        }
        if (status) status.textContent = "正在向供应商获取模型列表…";
        fetchButton.disabled = true;
        try {
          const payload = { base_url: url };
          const key = apiKey ? apiKey.value.trim() : "";
          if (key) payload.api_key = key;
          if (providerId) payload.provider_id = Number(providerId);
          const response = await fetch("/ai/models", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
          });
          const data = await response.json();
          if (!data.ok) throw new Error(data.error || "获取失败");
          populate(data.models, true);
          if (status) status.textContent = `已获取 ${data.models.length} 个模型，可在上方选择。`;
        } catch (error) {
          if (status) status.textContent = "获取模型失败：" + error.message;
        } finally {
          fetchButton.disabled = false;
        }
      });
    }

    syncPresetFromUrl();
    const initial = preset.selectedOptions[0];
    if (initial) {
      const models = (initial.dataset.models || "").split("|").filter(Boolean);
      if (models.length) populate(models, false);
      if (hint) hint.textContent = initial.dataset.hint || "";
    }
    if (!baseUrl.value.trim()) applyPreset(false);
  }

  function setupQuickModel(form) {
    const picker = form.querySelector("[data-model-picker]");
    const trigger = form.querySelector("[data-model-trigger]");
    const current = form.querySelector("[data-model-current]");
    const menu = form.querySelector("[data-model-menu]");
    const loading = form.querySelector("[data-model-loading]");
    const search = form.querySelector("[data-model-search]");
    const list = form.querySelector("[data-model-list]");
    const empty = form.querySelector("[data-model-empty]");
    const errorBox = form.querySelector("[data-model-error]");
    const refresh = form.querySelector("[data-model-refresh]");
    const hidden = form.querySelector("[data-model-value]");
    if (!picker || !trigger || !current || !menu || !loading || !search
        || !list || !empty || !errorBox || !refresh || !hidden) return;

    const providerId = form.dataset.providerId || "";
    const fallback = Array.from(new Set(
      (form.dataset.fallback || "").split("|").map((item) => item.trim()).filter(Boolean)
    ));
    let models = [];
    let loaded = false;
    let loadingNow = false;
    let open = false;

    function selectedModel() {
      return (hidden.value || "").trim();
    }

    function setCurrent(value) {
      const label = value || "选择模型";
      current.textContent = label;
      current.title = label;
    }

    function setOpen(next) {
      open = next;
      menu.hidden = !next;
      trigger.setAttribute("aria-expanded", String(next));
      picker.classList.toggle("open", next);
      if (!next) return;
      search.value = "";
      if (loaded) renderList();
      const rect = trigger.getBoundingClientRect();
      const menuWidth = menu.offsetWidth || 280;
      let offset = 0;
      if (rect.left + menuWidth > window.innerWidth - 12) {
        offset = window.innerWidth - 12 - menuWidth - rect.left;
      }
      if (rect.left + offset < 12) {
        offset = 12 - rect.left;
      }
      menu.style.left = `${offset}px`;
      search.focus();
    }

    function renderList() {
      const typed = (search.value || "").trim();
      const query = typed.toLowerCase();
      const currentValue = selectedModel();
      const visible = models.filter((name) => !query || name.toLowerCase().includes(query));
      list.innerHTML = "";
      visible.forEach((name) => {
        const option = document.createElement("button");
        option.type = "button";
        option.className = "quick-model-option";
        option.setAttribute("role", "option");
        option.setAttribute("aria-selected", String(name === currentValue));
        const label = document.createElement("span");
        label.className = "quick-model-option-label";
        label.textContent = name;
        option.appendChild(label);
        if (name === currentValue) {
          const check = document.createElement("span");
          check.className = "quick-model-check";
          check.textContent = "✓";
          option.appendChild(check);
        }
        option.addEventListener("click", () => applyModel(name));
        list.appendChild(option);
      });
      const custom = typed && !models.includes(typed);
      if (custom) {
        const option = document.createElement("button");
        option.type = "button";
        option.className = "quick-model-option quick-model-custom";
        option.setAttribute("role", "option");
        option.setAttribute("aria-selected", "false");
        const label = document.createElement("span");
        label.className = "quick-model-option-label";
        label.textContent = `使用自定义模型「${typed}」`;
        option.appendChild(label);
        option.addEventListener("click", () => applyModel(typed));
        list.appendChild(option);
      }
      empty.textContent = query ? "没有匹配的模型" : "暂无可选模型，可直接输入自定义名称";
      empty.hidden = Boolean(visible.length || custom);
      if (!loaded && loadingNow) empty.hidden = true;
    }

    function applyModel(name) {
      const value = String(name || "").trim();
      if (!value) return;
      if (value === selectedModel()) {
        setOpen(false);
        return;
      }
      hidden.value = value;
      setCurrent(value);
      setOpen(false);
      if (form.dataset.submitting === "1") return;
      form.dataset.submitting = "1";
      form.submit();
    }

    async function loadModels() {
      if (loadingNow) return;
      loadingNow = true;
      loaded = false;
      loading.hidden = false;
      errorBox.hidden = true;
      empty.hidden = true;
      refresh.disabled = true;
      trigger.classList.add("loading");
      trigger.setAttribute("aria-busy", "true");
      current.textContent = "获取模型中…";
      try {
        const payload = providerId
          ? { provider_id: Number(providerId) }
          : { base_url: (form.dataset.baseUrl || "").trim() };
        const response = await fetch("/ai/models", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        const data = await response.json();
        if (!response.ok || !data.ok) {
          throw new Error(data.error || "获取失败");
        }
        const fetched = Array.isArray(data.models) ? data.models : [];
        const source = fetched.length ? fetched : fallback;
        const merged = [];
        const seen = new Set();
        for (const raw of [selectedModel(), ...source]) {
          const value = String(raw || "").trim();
          if (!value || seen.has(value)) continue;
          seen.add(value);
          merged.push(value);
        }
        models = merged;
        loaded = true;
        errorBox.hidden = true;
        renderList();
      } catch (error) {
        models = fallback.slice();
        loaded = true;
        errorBox.textContent = "自动获取失败：" + error.message + "（可手动输入或点击重新获取）";
        errorBox.hidden = false;
        renderList();
      } finally {
        loadingNow = false;
        loading.hidden = true;
        refresh.disabled = false;
        trigger.classList.remove("loading");
        trigger.removeAttribute("aria-busy");
        setCurrent(selectedModel());
      }
    }

    trigger.addEventListener("click", async () => {
      if (open) {
        setOpen(false);
        return;
      }
      setOpen(true);
      if (!loaded && !loadingNow) await loadModels();
    });

    search.addEventListener("input", renderList);
    search.addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      const value = (search.value || "").trim();
      if (!value) return;
      applyModel(value);
    });

    menu.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      setOpen(false);
      trigger.focus();
    });

    refresh.addEventListener("click", (event) => {
      event.stopPropagation();
      loadModels();
    });

    document.addEventListener("click", (event) => {
      if (!picker.contains(event.target)) setOpen(false);
    });

    setCurrent(selectedModel());
  }

  document.querySelectorAll("form[data-model-form]").forEach(setupQuickModel);
  document.querySelectorAll("form.provider-form").forEach(setupProviderForm);

  // 展开 / 收起编辑面板
  document.querySelectorAll("[data-edit-toggle]").forEach((button) => {
    button.addEventListener("click", () => {
      const panel = document.getElementById(button.dataset.editToggle);
      if (!panel) return;
      const open = panel.hidden;
      panel.hidden = !open;
      button.setAttribute("aria-expanded", String(open));
      button.textContent = open ? "收起" : "编辑";
    });
  });

  // 快捷下拉与状态开关：变更后立即保存当前卡片
  document.addEventListener("change", (event) => {
    const control = event.target.closest("[data-auto-submit]");
    if (!control) return;
    const form = control.form;
    if (!form || form.dataset.submitting === "1") return;
    form.dataset.submitting = "1";
    form.submit();
  });
})();
