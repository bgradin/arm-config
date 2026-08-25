(() => {
  const page = () => document.querySelector("#page-content");

  function apiPath(path) {
    return path.startsWith("/jobs/") ? `/api${path}` : path;
  }

  async function replacePage(url) {
    const response = await fetch(url, {
      headers: { "X-Requested-With": "XMLHttpRequest" },
    });
    if (!response.ok) throw new Error(`Request failed (${response.status})`);
    const html = await response.text();
    const parsed = new DOMParser().parseFromString(html, "text/html");
    const next = parsed.querySelector("#page-content");
    if (!next || !page()) throw new Error("The server returned an invalid page");
    page().replaceWith(next);
    if (url !== window.location.pathname + window.location.search) {
      window.history.replaceState({}, "", url);
    }
  }

  async function submitForm(form, submitter) {
    const method = (form.getAttribute("method") || "get").toUpperCase();
    const action = submitter?.getAttribute("formaction") || form.getAttribute("action") || window.location.href;
    const formData = new FormData(form);
    if (submitter && submitter.name) formData.set(submitter.name, submitter.value);
    if (method === "GET") {
      const url = new URL(action, window.location.href);
      url.search = new URLSearchParams(formData).toString();
      await replacePage(`${url.pathname}${url.search}`);
      return;
    }
    const response = await fetch(apiPath(new URL(action, window.location.href).pathname), {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
      },
      body: new URLSearchParams(formData),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
    if (payload.deleted_job_id) {
      window.location.assign("/");
      return;
    }
    if (payload.job_id) {
      window.location.assign(`/jobs/${payload.job_id}`);
      return;
    }
    const current = window.location.pathname + window.location.search;
    await replacePage(current);
  }

  async function postSuggestion(action, body) {
    const response = await fetch(apiPath(new URL(action, window.location.href).pathname), {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
      },
      body,
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
    if (!payload.job) throw new Error("The server returned an invalid suggestion response");
    return payload.job;
  }

  function statusLabel(value) {
    return String(value || "").replaceAll("_", " ").replace(/^\w/, (letter) => letter.toUpperCase());
  }

  function suggestionValue(suggestion) {
    if (suggestion.kind === "show_name") return suggestion.value?.name;
    if (suggestion.kind === "media_type") return suggestion.value?.media_type;
    return suggestion.value?.season;
  }

  function renderFieldSuggestion(detail, field) {
    const wrapper = document.querySelector(`[data-field-suggestion="${field}"]`);
    if (!wrapper) return;
    wrapper.querySelectorAll("[data-xhr-suggestion]").forEach((button) => button.remove());
    const suggestion = detail.field_suggestions?.[field];
    if (!suggestion || suggestion.status !== "pending") return;
    const button = document.createElement("button");
    button.className = "suggestion-link";
    button.type = "button";
    button.dataset.xhrSuggestion = `${window.location.pathname}/suggestions/${suggestion.id}`;
    button.dataset.suggestionId = suggestion.id;
    button.dataset.suggestionAction = "accepted";
    button.textContent = `Suggested: ${suggestionValue(suggestion) ?? ""}`;
    wrapper.append(button);
  }

  function updateFields(detail) {
    const fields = ["resolved_media_type", "show_name", "season"];
    for (const field of fields) {
      const wrapper = document.querySelector(`[data-field-suggestion="${field}"]`);
      const control = wrapper?.querySelector(`[name="${field}"]`);
      if (control && Object.prototype.hasOwnProperty.call(detail.job, field)) {
        control.value = detail.job[field] ?? "";
      }
      renderFieldSuggestion(detail, field);
    }
  }

  function updateAssets(detail) {
    const assignments = new Map(
      (detail.assignments || []).map((assignment) => [String(assignment.asset_id), assignment]),
    );
    for (const asset of detail.assets || []) {
      const card = Array.from(document.querySelectorAll("[data-asset-id]")).find(
        (item) => item.dataset.assetId === String(asset.id),
      );
      if (!card) continue;
      const state = card.querySelector("[data-asset-state]");
      if (state) {
        state.className = `state state-${asset.disposition}`;
        state.textContent = statusLabel(asset.disposition);
      }
      const disposition = card.querySelector('[name="disposition"]');
      if (disposition) disposition.value = asset.disposition;
      const assignment = assignments.get(String(asset.id));
      if (!assignment) continue;
      const values = { ...assignment, edition_name: asset.edition_name };
      for (const [name, value] of Object.entries(values)) {
        const control = card.querySelector(`[name="${name}"]`);
        if (!control) continue;
        if (control.type === "checkbox") {
          control.checked = Boolean(asset.preferred);
        } else if (name !== "asset_id" && name !== "id" && name !== "job_id") {
          control.value = value ?? "";
        }
      }
    }
  }

  function updateSuggestionCard(detail, suggestionId) {
    const suggestion = (detail.suggestions || []).find(
      (item) => String(item.id) === String(suggestionId),
    );
    const card = Array.from(document.querySelectorAll("[data-suggestion-card]")).find(
      (item) => item.dataset.suggestionId === String(suggestionId),
    );
    if (!card) return;
    if (!suggestion) {
      card.remove();
      const list = document.querySelector("[data-suggestions-list]");
      if (list && !list.querySelector("[data-suggestion-card]")) {
        const empty = document.createElement("p");
        empty.className = "muted";
        empty.dataset.noSuggestions = "true";
        empty.textContent = "No suggestions.";
        list.append(empty);
      }
      return;
    }
    const form = card.querySelector("[data-suggestion-decision-form]");
    const state = document.createElement("span");
    state.className = "state";
    state.dataset.suggestionState = "true";
    state.textContent = statusLabel(suggestion.status);
    if (form) form.replaceWith(state);
    else {
      const previous = card.querySelector("[data-suggestion-state]");
      if (previous) previous.replaceWith(state);
    }
  }

  function applySuggestionUpdate(detail, suggestionId) {
    if (!detail?.job) throw new Error("The server returned an invalid job response");
    const state = document.querySelector("[data-job-state]");
    if (state) {
      state.className = `state state-${detail.job.state}`;
      state.textContent = statusLabel(detail.job.state);
    }
    const name = document.querySelector("[data-job-name]");
    if (name) name.textContent = detail.job.show_name || "Unidentified disc";
    updateSuggestionCard(detail, suggestionId);
    updateFields(detail);
    updateAssets(detail);
  }

  async function submitSuggestion(button) {
    const action = button.dataset.xhrSuggestion;
    if (!action) return;
    const detail = await postSuggestion(
      action,
      new URLSearchParams({ action: button.dataset.suggestionAction || "accepted" }),
    );
    applySuggestionUpdate(detail, button.dataset.suggestionId);
  }

  async function submitSuggestionForm(form, submitter) {
    const action = form.getAttribute("action");
    if (!action) return;
    const formData = new FormData(form);
    if (submitter?.name) formData.set(submitter.name, submitter.value);
    const detail = await postSuggestion(action, new URLSearchParams(formData));
    applySuggestionUpdate(detail, form.dataset.suggestionId);
  }

  function showError(error) {
    let notice = document.querySelector("[data-xhr-error]");
    if (!notice) {
      notice = document.createElement("div");
      notice.className = "notice error";
      notice.dataset.xhrError = "true";
      page()?.prepend(notice);
    }
    notice.textContent = error.message || String(error);
  }

  document.addEventListener("submit", async (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.matches("[data-xhr]")) return;
    const message = form.getAttribute("data-confirm");
    if (message && !window.confirm(message)) {
      event.preventDefault();
      return;
    }
    event.preventDefault();
    const button = event.submitter || form.querySelector("button[type=submit]:focus, button:not([type])");
    if (button) button.disabled = true;
    try {
      if (form.matches("[data-suggestion-form]")) {
        await submitSuggestionForm(form, event.submitter);
      } else {
        await submitForm(form, event.submitter);
      }
    } catch (error) {
      showError(error);
    } finally {
      if (button) button.disabled = false;
    }
  });

  document.addEventListener("click", async (event) => {
    const suggestion = event.target.closest("[data-xhr-suggestion]");
    if (suggestion) {
      event.preventDefault();
      if (suggestion.disabled) return;
      suggestion.disabled = true;
      try {
        await submitSuggestion(suggestion);
      } catch (error) {
        showError(error);
      } finally {
        suggestion.disabled = false;
      }
      return;
    }
    const toggle = event.target.closest(".task-tray-toggle");
    if (!toggle) return;
    const details = document.querySelector("#task-details");
    if (!details) return;
    const expanded = toggle.getAttribute("aria-expanded") === "true";
    toggle.setAttribute("aria-expanded", String(!expanded));
    details.hidden = expanded;
  });

  function renderTasks(payload) {
    const active = payload.tasks || [];
    const list = document.querySelector("[data-task-list]");
    const count = document.querySelector("[data-task-count]");
    const summary = document.querySelector("[data-task-summary]");
    const tray = document.querySelector("#task-tray");
    if (!list || !count || !summary || !tray) return;
    count.textContent = String(active.length);
    summary.textContent = active.length ? `${active.length} active` : "Idle";
    tray.classList.toggle("has-active", active.length > 0);
    list.replaceChildren();
    const tasks = (payload.recent || []).slice(0, 20);
    if (!tasks.length) {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = "No tasks yet.";
      list.append(empty);
      return;
    }
    for (const task of tasks) {
      const row = document.createElement("div");
      row.className = "task-row";
      const name = document.createElement("strong");
      name.textContent = String(task.kind || "task").replaceAll("_", " ");
      const state = document.createElement("span");
      state.className = `state state-${task.status}`;
      state.textContent = task.status;
      const error = document.createElement("p");
      error.className = "muted";
      error.textContent = task.error || (task.job_id ? `Job ${task.job_id.slice(0, 8)}` : "Site task");
      const timestamps = document.createElement("p");
      timestamps.className = "muted task-timestamps";
      const timeFormat = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });
      for (const [index, [label, value]] of [
        ["Created", task.created_at],
        ["Updated", task.updated_at],
      ].entries()) {
        if (!value) continue;
        if (index > 0 && timestamps.childNodes.length) timestamps.append(" · ");
        const time = document.createElement("time");
        time.dateTime = value;
        const date = new Date(value);
        time.textContent = `${label} ${Number.isNaN(date.getTime()) ? value : timeFormat.format(date)}`;
        timestamps.append(time);
      }
      row.append(name, state, timestamps, error);
      list.append(row);
    }
  }

  async function pollTasks() {
    try {
      const response = await fetch("/api/tasks", {
        headers: { "X-Requested-With": "XMLHttpRequest" },
      });
      if (response.ok) renderTasks(await response.json());
    } catch (_) {
      // The task indicator is intentionally quiet during a transient outage.
    }
  }

  pollTasks();
  window.setInterval(pollTasks, 1500);
})();
