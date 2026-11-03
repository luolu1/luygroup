(() => {
  "use strict";

  function setupProviderForm(form) {
    const preset = form.querySelector("select[data-preset]");
    // 注意：供应商模板的 <option> 也带有 data-base-url，这里必须限定到输入框。
    const baseUrl = form.querySelector("input[data-base-url]");
    const hint = form.querySelector("[data-preset-hint]");
    if (!preset || !baseUrl) return;

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
      if (hint) hint.textContent = option.dataset.hint || "选择供应商后会自动填入接口地址。";
    }

    preset.addEventListener("change", () => applyPreset(true));
    syncPresetFromUrl();
    const initial = preset.selectedOptions[0];
    if (initial && hint) hint.textContent = initial.dataset.hint || "";
    if (!baseUrl.value.trim()) applyPreset(false);
  }

  function setupModelSelect(options) {
    const { form, select, status, kind, allowEmpty, button } = options;
    const baseUrl = form.querySelector("input[data-base-url]");
    const apiKey = form.querySelector("input[data-api-key]");
    const preset = form.querySelector("select[data-preset]");
    const providerId = form.dataset.providerId || "";
    if (!select || !baseUrl) return;

    function presetFallback() {
      const option = preset ? preset.selectedOptions[0] : null;
      const raw = option ? option.dataset.models || "" : "";
      return raw.split("|").map((item) => item.trim()).filter(Boolean);
    }

    function setStatus(text) {
      if (status) status.textContent = text;
    }

    function current() {
      return select.dataset.current || select.value || "";
    }

    function render(models) {
      const keep = current();
      select.innerHTML = "";
      if (allowEmpty) {
        const empty = document.createElement("option");
        empty.value = "";
        empty.textContent = kind === "vision" ? "不使用图像模型" : "请选择模型";
        select.appendChild(empty);
      }
      models.forEach((name) => {
        const option = document.createElement("option");
        option.value = name;
        option.textContent = name;
        select.appendChild(option);
      });
      if (keep && !models.includes(keep)) {
        const extra = document.createElement("option");
        extra.value = keep;
        extra.textContent = `${keep}（当前）`;
        select.appendChild(extra);
      }
      if (keep && [...select.options].some((o) => o.value === keep)) {
        select.value = keep;
      } else if (models.length) {
        select.value = models[0];
      }
      select.dataset.current = select.value;
    }

    async function load(force) {
      const url = (baseUrl.value || "").trim();
      if (!url) {
        setStatus("请先填写接口地址。");
        return;
      }
      if (button) button.disabled = true;
      setStatus(kind === "vision" ? "正在获取图像模型…" : "正在获取模型…");
      try {
        const payload = { base_url: url, kind };
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
        const models = Array.isArray(data.models) ? data.models : [];
        render(models);
        setStatus(
          models.length
            ? `已获取 ${models.length} 个模型，可直接选择。`
            : "供应商没有返回可用模型。"
        );
      } catch (error) {
        const fallback = kind === "text" ? presetFallback() : [];
        if (fallback.length) {
          render(fallback);
          setStatus("自动获取失败：" + error.message + "，已显示内置候选模型。");
        } else {
          setStatus("获取模型失败：" + error.message);
        }
      } finally {
        if (button) button.disabled = false;
      }
    }

    select.addEventListener("change", () => {
      select.dataset.current = select.value;
    });
    if (button) button.addEventListener("click", () => load(true));
    load(false);
  }


  document.querySelectorAll("form.provider-form").forEach((form) => {
    setupProviderForm(form);
    setupModelSelect({
      form,
      select: form.querySelector("select[data-model-select]"),
      status: form.querySelector("[data-model-status]"),
      kind: "text",
      allowEmpty: false,
      button: form.querySelector("[data-fetch-models]"),
    });
    setupModelSelect({
      form,
      select: form.querySelector("select[data-vision-model-select]"),
      status: form.querySelector("[data-vision-model-status]"),
      kind: "vision",
      allowEmpty: true,
      button: form.querySelector("[data-fetch-vision-models]"),
    });
  });

  document.addEventListener("change", (event) => {
    const control = event.target.closest("[data-auto-submit]");
    if (!control) return;
    const form = control.form;
    if (!form || form.dataset.submitting === "1") return;
    form.dataset.submitting = "1";
    form.submit();
  });
})();
