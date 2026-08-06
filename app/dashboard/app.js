"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const state = {
  view: "overview",
  username: null,
  clients: [],
  providers: [],
  workers: [],
  jobs: [],
  selectedClientId: null,
  webhookClientId: null,
};

const viewMeta = {
  overview: ["System", "Overview"],
  jobs: ["Operations", "Jobs"],
  workers: ["Execution", "Workers"],
  providers: ["Configuration", "AI providers"],
  clients: ["Access", "Clients & API keys"],
  webhooks: ["Delivery", "Webhooks"],
  usage: ["Accounting", "Usage & cost"],
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function statusBadge(value) {
  const label = String(value || "unknown");
  const css = label.toLowerCase().replace(/[^a-z0-9_-]/g, "");
  return `<span class="status-badge ${css}">${escapeHtml(label)}</span>`;
}

function formatDate(value) {
  if (!value) return "—";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function relativeTime(value) {
  if (!value) return "Never";
  const seconds = Math.round((new Date(value).getTime() - Date.now()) / 1000);
  const formatter = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  const ranges = [
    [86400, "day"],
    [3600, "hour"],
    [60, "minute"],
  ];
  for (const [unitSeconds, unit] of ranges) {
    if (Math.abs(seconds) >= unitSeconds) return formatter.format(Math.round(seconds / unitSeconds), unit);
  }
  return formatter.format(seconds, "second");
}

function formatDuration(seconds) {
  const value = Math.max(0, Number(seconds || 0));
  if (value < 60) return `${value.toFixed(value < 10 ? 1 : 0)}s`;
  if (value < 3600) return `${Math.floor(value / 60)}m ${Math.round(value % 60)}s`;
  return `${Math.floor(value / 3600)}h ${Math.round((value % 3600) / 60)}m`;
}

function formatBytes(bytes) {
  const value = Number(bytes || 0);
  if (value < 1024) return `${value} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let size = value / 1024;
  let unit = units[0];
  for (let index = 1; index < units.length && size >= 1024; index += 1) {
    size /= 1024;
    unit = units[index];
  }
  return `${size.toFixed(size >= 10 ? 1 : 2)} ${unit}`;
}

function formatUsd(value, digits = 4) {
  const amount = Number(value || 0);
  return new Intl.NumberFormat(undefined, {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: amount < 0.01 ? digits : 2,
    maximumFractionDigits: amount < 0.01 ? digits : 2,
  }).format(amount);
}

function truncateId(value) {
  if (!value) return "—";
  return `${String(value).slice(0, 8)}…`;
}

async function api(path, options = {}) {
  const method = options.method || "GET";
  const headers = new Headers(options.headers || {});
  if (options.body !== undefined) headers.set("Content-Type", "application/json");
  headers.set("X-Media-Engine-Admin-UI", "1");
  const response = await fetch(path, {
    ...options,
    method,
    headers,
    credentials: "same-origin",
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
  if (response.status === 401 && path !== "/admin/session") showLogin();
  if (response.status === 204) {
    if (!response.ok) throw new Error(`Request failed with HTTP ${response.status}`);
    return null;
  }
  let payload = null;
  try { payload = await response.json(); } catch { payload = null; }
  if (!response.ok) {
    const detail = payload?.detail;
    throw new Error(Array.isArray(detail) ? detail.map((item) => item.msg).join(", ") : detail || `Request failed with HTTP ${response.status}`);
  }
  return payload;
}

function toast(message, type = "success") {
  const node = document.createElement("div");
  node.className = `toast ${type === "error" ? "error" : ""}`;
  node.textContent = message;
  $("#toast-region").append(node);
  setTimeout(() => node.remove(), 4200);
}

function showLogin(message = "") {
  $("#app-view").hidden = true;
  $("#login-view").hidden = false;
  const error = $("#login-error");
  error.textContent = message;
  error.hidden = !message;
}

function showApp() {
  $("#login-view").hidden = true;
  $("#app-view").hidden = false;
  $("#session-user").textContent = state.username || "Administrator";
}

function setUpdated() {
  $("#last-updated").textContent = `Updated ${new Intl.DateTimeFormat(undefined, { timeStyle: "short" }).format(new Date())}`;
}

function emptyState(title, copy) {
  return `<div class="empty-state"><div><h3>${escapeHtml(title)}</h3><p>${escapeHtml(copy)}</p></div></div>`;
}

function loadingCards(count = 4) {
  return Array.from({ length: count }, () => '<div class="skeleton"></div>').join("");
}

async function ensureClients() {
  state.clients = await api("/v2/admin/clients");
  const options = state.clients.map((client) => `<option value="${client.client_id}">${escapeHtml(client.name)}</option>`).join("");
  $("#job-client-filter").innerHTML = `<option value="">All clients</option>${options}`;
  $("#webhook-client-filter").innerHTML = options || '<option value="">No clients</option>';
  if (!state.selectedClientId || !state.clients.some((client) => client.client_id === state.selectedClientId)) {
    state.selectedClientId = state.clients[0]?.client_id || null;
  }
  if (!state.webhookClientId || !state.clients.some((client) => client.client_id === state.webhookClientId)) {
    state.webhookClientId = state.clients[0]?.client_id || null;
  }
  if (state.webhookClientId) $("#webhook-client-filter").value = state.webhookClientId;
}

async function switchView(name) {
  if (!viewMeta[name]) return;
  state.view = name;
  $$(".view").forEach((view) => view.classList.toggle("active", view.id === `view-${name}`));
  $$(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.view === name));
  $("#page-kicker").textContent = viewMeta[name][0];
  $("#page-title").textContent = viewMeta[name][1];
  $("#sidebar").classList.remove("open");
  try {
    await loaders[name]();
    setUpdated();
  } catch (error) {
    toast(error.message, "error");
  }
}

function jobsTable(items, compact = false) {
  if (!items.length) return emptyState("No jobs found", "Jobs matching this view will appear here.");
  return `<table><thead><tr><th>Job</th><th>Pipeline</th><th>Status</th>${compact ? "" : "<th>Stages</th><th>Duration</th>"}<th>Created</th><th></th></tr></thead><tbody>${items.map((job) => `
    <tr>
      <td><span class="row-title">${escapeHtml(job.source_filename)}</span><span class="row-subtitle">${escapeHtml(job.client_name)} · ${truncateId(job.job_id)}</span></td>
      <td><span class="row-title">${escapeHtml(job.pipeline)} v${escapeHtml(job.pipeline_version)}</span><span class="row-subtitle">${job.cache_hit ? "Cache hit" : job.run_reused ? "Shared run" : "New run"}</span></td>
      <td>${statusBadge(job.status)}${job.current_stage ? `<span class="row-subtitle">${escapeHtml(job.current_stage)}</span>` : ""}</td>
      ${compact ? "" : `<td><span class="row-title">${job.stage_completed}/${job.stage_total}</span><progress class="stage-progress" max="${job.stage_total || 1}" value="${job.stage_completed}">${job.stage_completed}/${job.stage_total}</progress></td><td>${formatDuration(job.duration_seconds)}</td>`}
      <td><span class="row-title">${relativeTime(job.created_at)}</span><span class="row-subtitle">${formatDate(job.created_at)}</span></td>
      <td><button class="button secondary small" data-job-id="${job.job_id}">Inspect</button></td>
    </tr>`).join("")}</tbody></table>`;
}

async function loadOverview() {
  $("#overview-metrics").innerHTML = loadingCards(4);
  const [overview, jobs, workers] = await Promise.all([
    api("/v2/admin/overview"),
    api("/v2/admin/jobs?limit=6"),
    api("/v2/admin/workers"),
  ]);
  const activeWorkers = overview.workers.online;
  $("#overview-metrics").innerHTML = `
    <article class="metric-card green"><span class="metric-label">Workers online</span><div class="metric-value">${activeWorkers}/${overview.workers.total}</div><span class="metric-note">${overview.active_stages} active stage${overview.active_stages === 1 ? "" : "s"}</span></article>
    <article class="metric-card"><span class="metric-label">Jobs running</span><div class="metric-value">${overview.jobs.running}</div><span class="metric-note">${overview.jobs.queued} queued · ${overview.jobs.total} total</span></article>
    <article class="metric-card purple"><span class="metric-label">AI cost · 24 hours</span><div class="metric-value">${formatUsd(overview.estimated_cost_usd_24h)}</div><span class="metric-note">${overview.usage_events_24h} provider responses</span></article>
    <article class="metric-card ${overview.webhook_events_attention ? "amber" : "green"}"><span class="metric-label">Delivery attention</span><div class="metric-value">${overview.webhook_events_attention}</div><span class="metric-note">Retrying or abandoned · 24 hours</span></article>`;
  $("#overview-jobs").innerHTML = jobsTable(jobs.items, true);
  $("#overview-workers").innerHTML = workers.length ? workers.slice(0, 6).map((worker) => `
    <div class="mini-row"><div><strong>${escapeHtml(worker.display_name)}</strong><small>${worker.active_stage ? `${escapeHtml(worker.active_stage.pipeline)} · ${escapeHtml(worker.active_stage.stage)}` : `Seen ${relativeTime(worker.last_seen_at)}`}</small></div>${statusBadge(worker.status)}</div>`).join("") : emptyState("No workers", "Registered workers will appear here.");
}

async function loadJobs() {
  if (!state.clients.length) await ensureClients();
  const params = new URLSearchParams({ limit: "200" });
  const status = $("#job-status-filter").value;
  const pipeline = $("#job-pipeline-filter").value;
  const client = $("#job-client-filter").value;
  if (status) params.set("status", status);
  if (pipeline) params.set("pipeline", pipeline);
  if (client) params.set("client_id", client);
  $("#jobs-table").innerHTML = loadingCards(4);
  const result = await api(`/v2/admin/jobs?${params}`);
  state.jobs = result.items;
  $("#job-count").textContent = `${result.total} job${result.total === 1 ? "" : "s"}`;
  $("#jobs-table").innerHTML = jobsTable(result.items);
}

function workerCapabilities(capabilities) {
  return Object.entries(capabilities || {}).flatMap(([group, values]) => {
    if (!Array.isArray(values)) return [];
    return values.map((value) => `<span class="chip" title="${escapeHtml(group)}">${escapeHtml(value)}</span>`);
  }).join("");
}

function workerDisplayStatus(worker) {
  return worker.desired_state === "active" ? worker.status : worker.desired_state;
}

function workerEnvironment(worker, token) {
  const releaseVersion = worker.release_version || "latest";
  const image = worker.profile === "rk1"
    ? `jimstro/media-engine:rk1-${releaseVersion}`
    : worker.profile === "jetson-xavier-nx"
      ? `jimstro/media-engine:jetson-xavier-nx-${releaseVersion}`
      : `jimstro/media-engine:${releaseVersion}`;
  return `# Save as .env.worker and keep it private
MEDIA_ENGINE_WORKER_API_URL=${worker.worker_api_url || window.location.origin}
MEDIA_ENGINE_WORKER_TOKEN=${token}
MEDIA_ENGINE_WORKER_IMAGE=${image}`;
}

async function loadWorkers() {
  $("#workers-grid").innerHTML = loadingCards(3);
  state.workers = await api("/v2/admin/workers");
  $("#workers-grid").innerHTML = state.workers.length ? state.workers.map((worker) => `
    <article class="worker-card">
      <div class="entity-head"><div class="entity-title"><div class="entity-icon">${escapeHtml(worker.capabilities?.backends?.[0] || worker.profile)}</div><div><h3>${escapeHtml(worker.display_name)}</h3><p>${escapeHtml(worker.worker_key)}</p></div></div>${statusBadge(workerDisplayStatus(worker))}</div>
      <div class="meta-grid"><div><span>Profile</span><strong>${escapeHtml(worker.profile)}</strong></div><div><span>Last heartbeat</span><strong>${relativeTime(worker.last_seen_at)}</strong></div><div><span>Runtime</span><strong>${escapeHtml(worker.runtime?.version || "Not connected")}</strong></div><div><span>Host</span><strong>${escapeHtml(worker.runtime?.hostname || "—")}</strong></div><div><span>Token</span><strong>${worker.credential_prefix ? `mew_${escapeHtml(worker.credential_prefix)}_…` : "Not enrolled"}</strong></div><div><span>Last authenticated</span><strong>${relativeTime(worker.credential_last_used_at)}</strong></div></div>
      <div class="capability-list">${workerCapabilities(worker.capabilities) || '<span class="chip">No capabilities reported</span>'}</div>
      ${worker.active_stage ? `<div class="active-lease"><strong>${escapeHtml(worker.active_stage.pipeline)} · ${escapeHtml(worker.active_stage.stage)}</strong><span>Attempt ${worker.active_stage.attempt} · ${Math.round((worker.active_stage.progress || 0) * 100)}% · lease ${relativeTime(worker.active_stage.lease_expires_at)}</span></div>` : `<div class="active-lease"><strong>${worker.desired_state === "active" ? "Ready for work" : worker.desired_state === "draining" ? "Draining" : "Access revoked"}</strong><span>No active stage lease</span></div>`}
      <div class="card-actions"><button class="button secondary small" data-worker-action="edit" data-id="${worker.worker_id}">Edit</button>${worker.desired_state === "active" ? `<button class="button secondary small" data-worker-action="drain" data-id="${worker.worker_id}">Drain</button>` : worker.desired_state === "draining" ? `<button class="button secondary small" data-worker-action="activate" data-id="${worker.worker_id}">Resume</button>` : ""}<button class="button secondary small" data-worker-action="rotate" data-id="${worker.worker_id}">Rotate token</button>${worker.desired_state === "revoked" ? `<button class="button danger small" data-worker-action="remove" data-id="${worker.worker_id}">Remove</button>` : `<button class="button danger small" data-worker-action="revoke" data-id="${worker.worker_id}">Revoke</button>`}</div>
    </article>`).join("") : emptyState("No workers registered", "Start a worker to see its heartbeat and capabilities.");
}

function workerForm(worker = null) {
  openForm({
    kicker: worker ? "Worker identity" : "New execution node",
    title: worker ? `Edit ${worker.display_name}` : "Add worker",
    submitLabel: worker ? "Save changes" : "Create worker",
    html: `<label><span>Display name</span><input name="display_name" value="${escapeHtml(worker?.display_name || "")}" required maxlength="255" placeholder="Amsterdam CPU worker 01"></label><label><span>Worker profile</span><input name="profile" list="worker-profile-options" value="${escapeHtml(worker?.profile || "cpu")}" required maxlength="64" pattern="[a-z0-9][a-z0-9._-]{0,63}"><datalist id="worker-profile-options"><option value="cpu"><option value="rk1"><option value="jetson-xavier-nx"><option value="intel-qsv"><option value="amd-vaapi"></datalist></label>${worker ? "" : '<label><span>Token expiry (optional)</span><input name="expires_at" type="datetime-local"></label>'}`,
    onSubmit: async (data) => {
      const payload = { display_name: data.get("display_name"), profile: data.get("profile") };
      if (worker) {
        await api(`/v2/admin/workers/${worker.worker_id}`, { method: "PATCH", body: payload });
        toast("Worker updated");
      } else {
        const expiresAt = data.get("expires_at");
        payload.expires_at = expiresAt ? new Date(expiresAt).toISOString() : null;
        const created = await api("/v2/admin/workers", { method: "POST", body: payload });
        showSecret("Save this worker configuration", "The token cannot be retrieved after you close this window. Put these values in .env.worker on the node.", workerEnvironment(created, created.worker_token));
      }
      await loadWorkers();
    },
  });
}

async function workerAction(action, id) {
  const worker = state.workers.find((item) => item.worker_id === id);
  if (!worker) return;
  if (action === "edit") return workerForm(worker);
  if (action === "drain") {
    await api(`/v2/admin/workers/${id}/drain`, { method: "POST" });
    toast("Worker is draining and will not receive new work");
  } else if (action === "activate") {
    await api(`/v2/admin/workers/${id}/activate`, { method: "POST" });
    toast("Worker resumed");
  } else if (action === "rotate") {
    if (!confirm(`Rotate the token for ${worker.display_name}? The current token will stop working immediately.`)) return;
    const rotated = await api(`/v2/admin/workers/${id}/rotate-token`, { method: "POST", body: { expires_at: null } });
    showSecret("Save the new worker configuration", "Replace the node's current token and restart its container.", workerEnvironment(rotated, rotated.worker_token));
  } else if (action === "revoke") {
    if (!confirm(`Revoke ${worker.display_name}? It will immediately lose access to the worker API.`)) return;
    await api(`/v2/admin/workers/${id}/revoke`, { method: "POST" });
    toast("Worker access revoked");
  } else if (action === "remove") {
    if (!confirm(`Permanently remove ${worker.display_name} from the worker fleet? Historical job results remain available.`)) return;
    await api(`/v2/admin/workers/${id}`, { method: "DELETE" });
    toast("Worker removed");
  }
  await loadWorkers();
}

function providerCard(provider) {
  return `<article class="entity-card">
    <div class="entity-head"><div class="entity-title"><div class="entity-icon">${escapeHtml(provider.provider)}</div><div><h3>${escapeHtml(provider.name)}</h3><p>${escapeHtml(provider.base_url)}</p></div></div>${statusBadge(provider.enabled ? "enabled" : "disabled")}</div>
    <div class="meta-grid"><div><span>Credential</span><strong>${escapeHtml(provider.credential_hint)}</strong></div><div><span>Default</span><strong>${provider.is_default ? "Yes" : "No"}</strong></div><div><span>Vision</span><strong>${escapeHtml(provider.models.vision)}</strong></div><div><span>Summary</span><strong>${escapeHtml(provider.models.summary)}</strong></div><div><span>Last verified</span><strong>${provider.last_verified_at ? relativeTime(provider.last_verified_at) : "Not verified"}</strong></div><div><span>Retries</span><strong>${provider.max_retries}</strong></div></div>
    ${provider.last_error ? `<p class="form-error">${escapeHtml(provider.last_error)}</p>` : ""}
    <div class="card-actions"><button class="button secondary small" data-provider-action="edit" data-id="${provider.provider_config_id}">Edit</button><button class="button secondary small" data-provider-action="verify" data-id="${provider.provider_config_id}">Verify</button>${provider.is_default ? "" : `<button class="button secondary small" data-provider-action="default" data-id="${provider.provider_config_id}">Make default</button>`}<button class="button secondary small" data-provider-action="toggle" data-id="${provider.provider_config_id}">${provider.enabled ? "Disable" : "Enable"}</button><button class="button danger small" data-provider-action="delete" data-id="${provider.provider_config_id}">Remove</button></div>
  </article>`;
}

async function loadProviders() {
  $("#providers-grid").innerHTML = loadingCards(2);
  state.providers = await api("/v2/admin/providers");
  $("#providers-grid").innerHTML = state.providers.length ? state.providers.map(providerCard).join("") : emptyState("No AI providers", "Add OpenAI or xAI credentials to enable understanding pipelines.");
}

function openForm({ kicker = "Configure", title, html, submitLabel = "Save", onSubmit }) {
  const dialog = $("#form-dialog");
  $("#form-dialog-kicker").textContent = kicker;
  $("#form-dialog-title").textContent = title;
  const form = $("#dynamic-form");
  form.innerHTML = `${html}<p class="form-error" data-form-error hidden></p><div class="dialog-actions"><button type="button" class="button secondary" data-close-dialog="form-dialog">Cancel</button><button type="submit" class="button primary">${escapeHtml(submitLabel)}</button></div>`;
  form.onsubmit = async (event) => {
    event.preventDefault();
    const submit = $("button[type='submit']", form);
    const error = $("[data-form-error]", form);
    submit.disabled = true;
    error.hidden = true;
    try {
      await onSubmit(new FormData(form));
      dialog.close();
    } catch (caught) {
      error.textContent = caught.message;
      error.hidden = false;
    } finally {
      submit.disabled = false;
    }
  };
  dialog.showModal();
}

function providerForm(provider = null) {
  const models = provider?.models || {};
  openForm({
    kicker: provider ? "Provider connection" : "New model connection",
    title: provider ? `Edit ${provider.name}` : "Add AI provider",
    submitLabel: provider ? "Save changes" : "Verify and add",
    html: `<div class="form-grid">
      <label><span>Name</span><input name="name" value="${escapeHtml(provider?.name || "")}" required maxlength="255"></label>
      <label><span>Provider</span><select name="provider" ${provider ? "disabled" : ""}><option value="xai" ${provider?.provider === "xai" ? "selected" : ""}>xAI</option><option value="openai" ${provider?.provider === "openai" ? "selected" : ""}>OpenAI</option></select></label>
      <label class="full"><span>API key ${provider ? "(leave empty to keep the current key)" : ""}</span><input name="api_key" type="password" autocomplete="new-password" ${provider ? "" : "required"}></label>
      <label class="full"><span>Base URL ${provider ? "" : "(optional)"}</span><input name="base_url" type="url" value="${escapeHtml(provider?.base_url || "")}" placeholder="Use provider default"></label>
      <label><span>Transcription model</span><input name="transcription" value="${escapeHtml(models.transcription || "")}" placeholder="Provider default"></label>
      <label><span>Planning model</span><input name="planning" value="${escapeHtml(models.planning || "")}" placeholder="Provider default"></label>
      <label><span>Vision model</span><input name="vision" value="${escapeHtml(models.vision || "")}" placeholder="Provider default"></label>
      <label><span>Summary model</span><input name="summary" value="${escapeHtml(models.summary || "")}" placeholder="Provider default"></label>
      <label><span>Timeout seconds</span><input name="timeout_seconds" type="number" min="10" max="3600" value="${provider?.timeout_seconds || 600}"></label>
      <label><span>Maximum retries</span><input name="max_retries" type="number" min="0" max="10" value="${provider?.max_retries ?? 2}"></label>
      <div class="check-row"><label><input type="checkbox" name="enabled" ${provider?.enabled === false ? "" : "checked"}> Enabled</label><label><input type="checkbox" name="is_default" ${provider?.is_default ? "checked" : ""}> Default provider</label></div>
    </div>`,
    onSubmit: async (data) => {
      const payload = {
        name: data.get("name"),
        timeout_seconds: Number(data.get("timeout_seconds")),
        max_retries: Number(data.get("max_retries")),
        enabled: data.has("enabled"),
        is_default: data.has("is_default"),
      };
      const apiKey = data.get("api_key");
      const baseUrl = data.get("base_url");
      if (apiKey) payload.api_key = apiKey;
      if (baseUrl) payload.base_url = baseUrl;
      const modelNames = ["transcription", "planning", "vision", "summary"];
      if (modelNames.every((name) => data.get(name))) payload.models = Object.fromEntries(modelNames.map((name) => [name, data.get(name)]));
      if (!provider) payload.provider = data.get("provider");
      await api(provider ? `/v2/admin/providers/${provider.provider_config_id}` : "/v2/admin/providers", {
        method: provider ? "PATCH" : "POST",
        body: payload,
      });
      toast(provider ? "Provider updated" : "Provider verified and added");
      await loadProviders();
    },
  });
}

async function providerAction(action, id) {
  const provider = state.providers.find((item) => item.provider_config_id === id);
  if (!provider) return;
  if (action === "edit") return providerForm(provider);
  if (action === "delete") {
    if (!confirm(`Remove ${provider.name}? Existing usage history will be retained.`)) return;
    await api(`/v2/admin/providers/${id}`, { method: "DELETE" });
    toast("Provider removed");
  } else if (action === "verify") {
    await api(`/v2/admin/providers/${id}/verify`, { method: "POST" });
    toast("Provider credentials verified");
  } else if (action === "default") {
    await api(`/v2/admin/providers/${id}`, { method: "PATCH", body: { enabled: true, is_default: true } });
    toast("Default provider changed");
  } else if (action === "toggle") {
    await api(`/v2/admin/providers/${id}`, { method: "PATCH", body: { enabled: !provider.enabled } });
    toast(provider.enabled ? "Provider disabled" : "Provider enabled");
  }
  await loadProviders();
}

async function loadClients() {
  await ensureClients();
  $("#clients-list").innerHTML = state.clients.length ? state.clients.map((client) => `
    <button class="selection-item ${client.client_id === state.selectedClientId ? "active" : ""}" data-client-id="${client.client_id}"><span><strong>${escapeHtml(client.name)}</strong><small>${truncateId(client.client_id)}</small></span>${statusBadge(client.enabled ? "enabled" : "disabled")}</button>`).join("") : emptyState("No clients", "Add a client project to issue scoped API keys.");
  if (state.selectedClientId) await loadClientDetail(state.selectedClientId);
  else $("#client-detail").innerHTML = emptyState("No client selected", "Add a client project to continue.");
}

async function loadClientDetail(clientId) {
  state.selectedClientId = clientId;
  $$(".selection-item").forEach((item) => item.classList.toggle("active", item.dataset.clientId === clientId));
  const client = state.clients.find((item) => item.client_id === clientId);
  const keys = await api(`/v2/admin/clients/${clientId}/api-keys`);
  $("#client-detail").className = "client-detail";
  $("#client-detail").innerHTML = `<div class="client-detail-head"><div><p class="eyebrow">${escapeHtml(truncateId(clientId))}</p><h2>${escapeHtml(client?.name || "Client")}</h2></div><button class="button primary small" id="create-key-button">Create API key</button></div>
    <div class="key-list">${keys.length ? keys.map((key) => `<div class="key-item"><div><strong>${escapeHtml(key.name)} · me_${escapeHtml(key.key_prefix)}_…</strong><small>${escapeHtml(key.scopes.join(" · "))} · ${key.revoked_at ? `revoked ${relativeTime(key.revoked_at)}` : key.last_used_at ? `used ${relativeTime(key.last_used_at)}` : "never used"}</small></div>${key.revoked_at ? statusBadge("disabled") : `<button class="button danger small" data-revoke-key="${key.api_key_id}">Revoke</button>`}</div>`).join("") : emptyState("No API keys", "Create a scoped key for this product integration.")}</div>`;
  $("#create-key-button")?.addEventListener("click", () => apiKeyForm(client));
}

function clientForm() {
  openForm({
    kicker: "Product access",
    title: "Add client project",
    submitLabel: "Create client",
    html: `<label><span>Client name</span><input name="name" required maxlength="255" placeholder="Telegram bot"></label><label class="check-row"><input type="checkbox" name="enabled" checked> Enabled immediately</label>`,
    onSubmit: async (data) => {
      const client = await api("/v2/admin/clients", { method: "POST", body: { name: data.get("name"), enabled: data.has("enabled") } });
      state.selectedClientId = client.client_id;
      toast("Client project created");
      await loadClients();
    },
  });
}

function showSecret(title, copy, secret) {
  $("#secret-title").textContent = title;
  $("#secret-copy").textContent = copy;
  $("#secret-value").textContent = secret;
  $("#secret-dialog").showModal();
}

function apiKeyForm(client) {
  openForm({
    kicker: client.name,
    title: "Create API key",
    submitLabel: "Create key",
    html: `<label><span>Key name</span><input name="name" required maxlength="255" placeholder="Production"></label><div class="check-row"><label><input type="checkbox" name="scope" value="assets:read" checked> Read assets</label><label><input type="checkbox" name="scope" value="assets:write" checked> Upload assets</label><label><input type="checkbox" name="scope" value="jobs:read" checked> Read jobs</label><label><input type="checkbox" name="scope" value="jobs:write" checked> Submit jobs</label></div>`,
    onSubmit: async (data) => {
      const scopes = data.getAll("scope");
      if (!scopes.length) throw new Error("Select at least one permission");
      const created = await api(`/v2/admin/clients/${client.client_id}/api-keys`, { method: "POST", body: { name: data.get("name"), scopes } });
      showSecret("Save this API key", "It cannot be retrieved after you close this window.", created.api_key);
      await loadClientDetail(client.client_id);
    },
  });
}

async function loadWebhooks() {
  await ensureClients();
  if (!state.webhookClientId) {
    $("#webhook-endpoints").innerHTML = emptyState("No clients", "Create a client before registering webhook endpoints.");
    $("#webhook-events").innerHTML = "";
    return;
  }
  $("#webhook-client-filter").value = state.webhookClientId;
  const [endpoints, events] = await Promise.all([
    api(`/v2/admin/clients/${state.webhookClientId}/webhook-endpoints`),
    api(`/v2/admin/webhook-events?client_id=${state.webhookClientId}&limit=100`),
  ]);
  $("#webhook-endpoints").innerHTML = endpoints.length ? endpoints.map((endpoint) => `<article class="entity-card">
    <div class="entity-head"><div class="entity-title"><div class="entity-icon">WH</div><div><h3>${escapeHtml(endpoint.name)}</h3><p>${escapeHtml(endpoint.url)}</p></div></div>${statusBadge(endpoint.enabled ? "enabled" : "disabled")}</div>
    <div class="meta-grid"><div><span>Default</span><strong>${endpoint.is_default ? "Yes" : "No"}</strong></div><div><span>Secret</span><strong>${escapeHtml(endpoint.signing_secret_hint)}</strong></div><div><span>Events</span><strong>${escapeHtml(endpoint.events.join(", "))}</strong></div><div><span>Updated</span><strong>${relativeTime(endpoint.updated_at)}</strong></div></div>
    <div class="card-actions"><button class="button secondary small" data-webhook-action="test" data-id="${endpoint.webhook_endpoint_id}">Send test</button>${endpoint.is_default ? "" : `<button class="button secondary small" data-webhook-action="default" data-id="${endpoint.webhook_endpoint_id}">Make default</button>`}<button class="button secondary small" data-webhook-action="rotate" data-id="${endpoint.webhook_endpoint_id}">Rotate secret</button><button class="button ${endpoint.enabled ? "danger" : "secondary"} small" data-webhook-action="toggle" data-enabled="${endpoint.enabled}" data-id="${endpoint.webhook_endpoint_id}">${endpoint.enabled ? "Disable" : "Enable"}</button></div>
  </article>`).join("") : emptyState("No webhook endpoints", "Register a signed destination for optional terminal job notifications.");
  $("#webhook-events").innerHTML = events.length ? `<table><thead><tr><th>Event</th><th>Status</th><th>Attempts</th><th>Job</th><th>Created</th><th>Error</th></tr></thead><tbody>${events.map((event) => `<tr><td><span class="row-title">${escapeHtml(event.event_type)}</span><span class="row-subtitle">${truncateId(event.event_id)}</span></td><td>${statusBadge(event.status)}</td><td>${event.attempt_count}</td><td class="mono">${truncateId(event.job_id)}</td><td>${relativeTime(event.created_at)}</td><td><span class="row-title">${escapeHtml(event.last_error || "—")}</span></td></tr>`).join("")}</tbody></table>` : emptyState("No delivery events", "Test events and terminal job notifications will appear here.");
}

function webhookForm() {
  const client = state.clients.find((item) => item.client_id === state.webhookClientId);
  if (!client) return toast("Create or select a client first", "error");
  openForm({
    kicker: client.name,
    title: "Add webhook endpoint",
    submitLabel: "Register endpoint",
    html: `<label><span>Name</span><input name="name" required maxlength="255" placeholder="Production notifications"></label><label><span>HTTPS destination</span><input name="url" type="url" required placeholder="https://example.com/media-events"></label><div class="check-row"><label><input type="checkbox" name="event" value="job.completed" checked> Job completed</label><label><input type="checkbox" name="event" value="job.failed" checked> Job failed</label></div><div class="check-row"><label><input type="checkbox" name="is_default"> Use by default for new jobs</label></div>`,
    onSubmit: async (data) => {
      const events = data.getAll("event");
      if (!events.length) throw new Error("Select at least one event type");
      const created = await api(`/v2/admin/clients/${client.client_id}/webhook-endpoints`, { method: "POST", body: { name: data.get("name"), url: data.get("url"), events, enabled: true, is_default: data.has("is_default") } });
      showSecret("Save the signing secret", "Use it to verify the HMAC signature on every delivery.", created.signing_secret);
      await loadWebhooks();
    },
  });
}

async function webhookAction(action, id, enabled) {
  if (action === "test") {
    await api(`/v2/admin/webhook-endpoints/${id}/test`, { method: "POST" });
    toast("Test event queued");
    setTimeout(() => loadWebhooks().catch((error) => toast(error.message, "error")), 1100);
    return;
  }
  if (action === "default") {
    await api(`/v2/admin/webhook-endpoints/${id}`, { method: "PATCH", body: { enabled: true, is_default: true } });
    toast("Default endpoint changed");
  } else if (action === "rotate") {
    if (!confirm("Rotate this signing secret? The previous secret will stop working immediately.")) return;
    const rotated = await api(`/v2/admin/webhook-endpoints/${id}/rotate-secret`, { method: "POST" });
    showSecret("Save the new signing secret", "The previous secret is no longer valid.", rotated.signing_secret);
  } else if (action === "toggle") {
    if (enabled) await api(`/v2/admin/webhook-endpoints/${id}`, { method: "DELETE" });
    else await api(`/v2/admin/webhook-endpoints/${id}`, { method: "PATCH", body: { enabled: true } });
    toast(enabled ? "Webhook endpoint disabled" : "Webhook endpoint enabled");
  }
  await loadWebhooks();
}

async function loadUsage() {
  const provider = $("#usage-provider-filter").value;
  const events = await api(`/v2/admin/usage?limit=500${provider ? `&provider=${encodeURIComponent(provider)}` : ""}`);
  const total = events.reduce((sum, event) => sum + Number(event.estimated_cost_usd || 0), 0);
  $("#usage-summary").textContent = `${events.length} responses · ${formatUsd(total)} shown`;
  $("#usage-table").innerHTML = events.length ? `<table><thead><tr><th>Provider</th><th>Operation</th><th>Model</th><th>Outcome</th><th>Cost</th><th>Latency</th><th>Time</th></tr></thead><tbody>${events.map((event) => `<tr><td><span class="row-title">${escapeHtml(event.provider)}</span></td><td>${escapeHtml(event.operation)}</td><td><span class="row-title">${escapeHtml(event.model)}</span></td><td>${statusBadge(event.outcome)}</td><td>${event.estimated_cost_usd == null ? "—" : formatUsd(event.estimated_cost_usd, 6)}</td><td>${event.latency_ms} ms</td><td>${relativeTime(event.completed_at)}</td></tr>`).join("")}</tbody></table>` : emptyState("No usage events", "Provider calls will be accounted here.");
}

async function showJobDetail(jobId) {
  const dialog = $("#detail-dialog");
  $("#detail-title").textContent = `Job ${truncateId(jobId)}`;
  $("#detail-content").innerHTML = loadingCards(4);
  dialog.showModal();
  try {
    const detail = await api(`/v2/admin/jobs/${jobId}`);
    const job = detail.job;
    $("#detail-title").textContent = job.source_filename;
    $("#detail-content").innerHTML = `
      <section class="detail-section"><div class="detail-summary"><div><span>Status</span><strong>${statusBadge(job.status)}</strong></div><div><span>Pipeline</span><strong>${escapeHtml(job.pipeline)} v${escapeHtml(job.pipeline_version)}</strong></div><div><span>Client</span><strong>${escapeHtml(job.client_name)}</strong></div><div><span>AI cost</span><strong>${formatUsd(detail.estimated_cost_usd, 6)}</strong></div><div><span>Duration</span><strong>${formatDuration(job.duration_seconds)}</strong></div><div><span>Cache</span><strong>${job.cache_hit ? "Hit" : job.run_reused ? "Shared run" : "New run"}</strong></div><div><span>Source</span><strong>${formatBytes(detail.source_size_bytes)} · ${escapeHtml(detail.source_media_type || "unknown")}</strong></div><div><span>Expires</span><strong>${formatDate(detail.run_expires_at)}</strong></div></div></section>
      <section class="detail-section"><h3>Stages</h3><div class="table-wrap"><table><thead><tr><th>Stage</th><th>Status</th><th>Attempt</th><th>Worker</th><th>Timing</th><th>Error</th></tr></thead><tbody>${detail.stages.map((stage) => `<tr><td><span class="row-title">${escapeHtml(stage.name)}</span></td><td>${statusBadge(stage.status)}</td><td>${stage.attempt}/${stage.max_attempts}</td><td>${escapeHtml(stage.worker_name || "—")}</td><td>${stage.started_at ? formatDuration((new Date(stage.completed_at || Date.now()) - new Date(stage.started_at)) / 1000) : "—"}</td><td><span class="row-title">${escapeHtml(stage.error_message || "—")}</span></td></tr>`).join("")}</tbody></table></div></section>
      <section class="detail-section"><h3>Artifacts · ${detail.artifacts.length}</h3><div class="table-wrap">${detail.artifacts.length ? `<table><thead><tr><th>Type</th><th>Format</th><th>Size</th><th>Storage</th><th>Expires</th></tr></thead><tbody>${detail.artifacts.map((artifact) => `<tr><td><span class="row-title">${escapeHtml(artifact.artifact_type)}</span><span class="row-subtitle mono">${truncateId(artifact.sha256)}</span></td><td>${escapeHtml(artifact.format)}</td><td>${formatBytes(artifact.size_bytes)}</td><td>${statusBadge(artifact.storage_state)}</td><td>${formatDate(artifact.expires_at)}</td></tr>`).join("")}</tbody></table>` : emptyState("No artifacts yet", "Outputs will appear as stages complete.")}</div></section>
      <section class="detail-section"><h3>Provider usage · ${detail.usage.length}</h3><div class="table-wrap">${detail.usage.length ? `<table><thead><tr><th>Operation</th><th>Provider</th><th>Model</th><th>Outcome</th><th>Cost</th></tr></thead><tbody>${detail.usage.map((event) => `<tr><td>${escapeHtml(event.operation)}</td><td>${escapeHtml(event.provider)}</td><td><span class="row-title">${escapeHtml(event.model)}</span></td><td>${statusBadge(event.outcome)}</td><td>${event.estimated_cost_usd == null ? "—" : formatUsd(event.estimated_cost_usd, 6)}</td></tr>`).join("")}</tbody></table>` : emptyState("No provider usage", "This job has no model-backed stages yet.")}</div></section>
      <section class="detail-section"><h3>Run configuration</h3><pre class="json-block">${escapeHtml(JSON.stringify({ run_key: detail.run_key, options: detail.options, processor_versions: detail.processor_versions, source_metadata: detail.source_metadata }, null, 2))}</pre></section>`;
  } catch (error) {
    $("#detail-content").innerHTML = emptyState("Unable to load job", error.message);
  }
}

const loaders = {
  overview: loadOverview,
  jobs: loadJobs,
  workers: loadWorkers,
  providers: loadProviders,
  clients: loadClients,
  webhooks: loadWebhooks,
  usage: loadUsage,
};

$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = $("button[type='submit']", form);
  const error = $("#login-error");
  button.disabled = true;
  error.hidden = true;
  try {
    await api("/admin/session", { method: "POST", body: { username: form.username.value, password: form.password.value } });
    const session = await api("/admin/session");
    state.username = session.username;
    form.password.value = "";
    showApp();
    await ensureClients();
    await switchView("overview");
  } catch (caught) {
    error.textContent = caught.message;
    error.hidden = false;
  } finally {
    button.disabled = false;
  }
});

$("#logout-button").addEventListener("click", async () => {
  try { await api("/admin/session", { method: "DELETE" }); } catch { /* session is gone */ }
  state.username = null;
  showLogin();
});

$("#refresh-button").addEventListener("click", () => switchView(state.view));
$("#menu-button").addEventListener("click", () => $("#sidebar").classList.toggle("open"));
$("#add-provider-button").addEventListener("click", () => providerForm());
$("#add-worker-button").addEventListener("click", () => workerForm());
$("#add-client-button").addEventListener("click", clientForm);
$("#add-webhook-button").addEventListener("click", webhookForm);
$("#job-status-filter").addEventListener("change", loadJobs);
$("#job-pipeline-filter").addEventListener("change", loadJobs);
$("#job-client-filter").addEventListener("change", loadJobs);
$("#usage-provider-filter").addEventListener("change", loadUsage);
$("#webhook-client-filter").addEventListener("change", (event) => {
  state.webhookClientId = event.target.value;
  loadWebhooks().catch((error) => toast(error.message, "error"));
});

$("#copy-secret-button").addEventListener("click", async () => {
  const value = $("#secret-value").textContent;
  try {
    await navigator.clipboard.writeText(value);
    toast("Copied to clipboard");
  } catch {
    const range = document.createRange();
    range.selectNodeContents($("#secret-value"));
    window.getSelection().removeAllRanges();
    window.getSelection().addRange(range);
    toast("Secret selected — copy it now");
  }
});

document.addEventListener("click", async (event) => {
  const viewButton = event.target.closest("[data-view]");
  if (viewButton) {
    await switchView(viewButton.dataset.view);
    return;
  }
  const closeButton = event.target.closest("[data-close-dialog]");
  if (closeButton) {
    $(`#${closeButton.dataset.closeDialog}`).close();
    return;
  }
  const jobButton = event.target.closest("[data-job-id]");
  if (jobButton) return showJobDetail(jobButton.dataset.jobId);
  const clientButton = event.target.closest("[data-client-id]");
  if (clientButton) return loadClientDetail(clientButton.dataset.clientId).catch((error) => toast(error.message, "error"));
  const revokeButton = event.target.closest("[data-revoke-key]");
  if (revokeButton) {
    if (!confirm("Revoke this API key? Existing integrations using it will stop immediately.")) return;
    try {
      await api(`/v2/admin/api-keys/${revokeButton.dataset.revokeKey}`, { method: "DELETE" });
      toast("API key revoked");
      await loadClientDetail(state.selectedClientId);
    } catch (error) { toast(error.message, "error"); }
    return;
  }
  const providerButton = event.target.closest("[data-provider-action]");
  if (providerButton) {
    try { await providerAction(providerButton.dataset.providerAction, providerButton.dataset.id); }
    catch (error) { toast(error.message, "error"); }
    return;
  }
  const workerButton = event.target.closest("[data-worker-action]");
  if (workerButton) {
    try { await workerAction(workerButton.dataset.workerAction, workerButton.dataset.id); }
    catch (error) { toast(error.message, "error"); }
    return;
  }
  const webhookButton = event.target.closest("[data-webhook-action]");
  if (webhookButton) {
    try { await webhookAction(webhookButton.dataset.webhookAction, webhookButton.dataset.id, webhookButton.dataset.enabled === "true"); }
    catch (error) { toast(error.message, "error"); }
  }
});

async function bootstrap() {
  try {
    const session = await api("/admin/session");
    state.username = session.username;
    showApp();
    await ensureClients();
    await switchView("overview");
  } catch {
    showLogin();
  }
}

bootstrap();
