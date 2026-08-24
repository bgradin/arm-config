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
    const method = (form.method || "get").toUpperCase();
    const action = form.action || window.location.href;
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
    if (payload.job_id) {
      window.location.assign(`/jobs/${payload.job_id}`);
      return;
    }
    const current = window.location.pathname + window.location.search;
    await replacePage(current);
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
    const button = form.querySelector("button[type=submit]:focus, button:not([type])");
    if (button) button.disabled = true;
    try {
      await submitForm(form, event.submitter);
    } catch (error) {
      showError(error);
    } finally {
      if (button) button.disabled = false;
    }
  });

  document.addEventListener("click", (event) => {
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
      row.append(name, state, error);
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
