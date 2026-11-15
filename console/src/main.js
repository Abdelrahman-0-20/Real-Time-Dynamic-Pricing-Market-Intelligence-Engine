// Pricing Console — thin client over the Pricing HTTP API.
//
// SECURITY MODEL (the Tauri skill's boundary rules):
//  * The webview has NO filesystem/shell/process power — its only capability
//    is the Tauri HTTP plugin scoped to http://127.0.0.1:8000 (deny-by-default
//    in capabilities/default.json).
//  * All validation, auth, and error shaping live server-side in the Python
//    API; this file only renders {error, fix} envelopes as-is.
//  * The API key lives in webview localStorage (local-only threat model for
//    the walking skeleton; swap for the Tauri store plugin before shipping).
import { fetch } from "@tauri-apps/plugin-http";

const $ = (sel) => document.querySelector(sel);
const state = {
  base: localStorage.getItem("pc.base") || "http://127.0.0.1:8000",
  key: localStorage.getItem("pc.key") || "",
  lastTrace: null,
};

// ---------------------------------------------------------------- transport

async function api(path, options = {}) {
  const response = await fetch(`${state.base}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      "X-API-Key": state.key,
      ...(options.headers || {}),
    },
  });
  const body = response.status === 204 ? null : await response.json();
  if (!response.ok) {
    const message = body?.error
      ? `${body.error}\nFix: ${body.fix || "—"}`
      : `HTTP ${response.status}: ${JSON.stringify(body).slice(0, 200)}`;
    throw new Error(message);
  }
  return body;
}

function showError(error) {
  $("#error").textContent = error?.message || String(error);
}
function clearError() {
  $("#error").textContent = "";
}
function esc(value) {
  return String(value).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])
  );
}
function fmt(value) {
  return typeof value === "number"
    ? Number.isInteger(value) ? value : value.toFixed(2)
    : String(value);
}

// ------------------------------------------------------------------- views

async function loadStatus() {
  const health = await api("/health");
  const pill = $("#conn-state");
  pill.textContent = health.auth === "dev-open" ? "dev-open" : "api-key";
  pill.className = `pill ${health.auth === "dev-open" ? "bad" : "ok"}`;
  $("#status-out").textContent = JSON.stringify(health, null, 2);
}

async function priceExplain() {
  const sku = $("#sku").value.trim();
  const market = $("#market").value.trim() || "EG";
  const body = await api("/v1/price/explain", {
    method: "POST",
    body: JSON.stringify({ sku, market }),
  });
  const d = body.decision;
  state.lastTrace = d.trace_id;
  const clamps = d.guardrail_actions.length
    ? d.guardrail_actions
        .map((a) => `${a.rule} ${fmt(a.from_price)}→${fmt(a.to_price)}`)
        .join(", ")
    : "none";
  $("#price-out").innerHTML = `
    <div class="kv">
      <p><b>${esc(d.served_price)} ${esc(d.currency)}</b> · ${esc(d.model_version)}
         · trigger ${esc(d.trigger)} · arm ${esc(d.policy_arm)}
         ${d.degraded ? '· <span class="degraded">DEGRADED</span>' : ""}</p>
      <p><b>unclamped</b> ${fmt(d.unclamped_price)} → <b>clamps</b> ${esc(clamps)}</p>
      <p><b>stale</b> ${esc(d.stale_features.join(", ") || "none")}
         · <b>dropped</b> ${esc(d.dropped_features.join(", ") || "none")}</p>
      <p><b>trace</b> ${esc(d.trace_id)}</p>
      <p><b>pack</b> ${esc(d.policy_pack.market)}/${esc(d.policy_pack.version)}
         (${esc(d.policy_pack.status)})</p>
    </div>
    <table><thead><tr><th>feature</th><th>contribution</th></tr></thead>
    <tbody>${body.contributions
      .slice(0, 10)
      .map(
        (c) =>
          `<tr><td>${esc(c.feature)}</td><td class="${
            c.contribution < 0 ? "contrib-neg" : "contrib-pos"
          }">${fmt(c.contribution)}</td></tr>`
      )
      .join("")}</tbody></table>`;
  $("#review-row").hidden = false;
}

async function submitReview(choice) {
  if (!state.lastTrace) return showError(new Error("price a SKU first — no decision to review"));
  const note = $("#review-note").value.trim() || null;
  const entry = await api(`/v1/decisions/${state.lastTrace}/review`, {
    method: "POST",
    body: JSON.stringify({ choice, note }),
  });
  clearError();
  $("#price-out").insertAdjacentHTML(
    "beforeend",
    `<p class="muted">review recorded: <b>${esc(entry.choice)}</b> at ${esc(
      entry.at
    )}</p>`
  );
}

async function loadSignals() {
  const body = await api("/v1/signals");
  const rows = body.signals
    .map(
      (s) =>
        `<tr><td>${esc(s.signal_type)}</td><td>${
          esc(s.subject?.sku || s.subject?.category || "?")
        }</td><td>${fmt(s.confidence)}</td><td>${fmt(s.source_trust)}</td></tr>`
    )
    .join("");
  const quarantines = body.quarantines
    .map((q) => `<li>${esc(q.reason)}: ${esc(q.detail)}</li>`)
    .join("");
  $("#signals-out").innerHTML = `
    <p class="muted">last crawl ${esc(body.ran_at)} · stats ${JSON.stringify(
      body.stats
    )}</p>
    <table><thead><tr><th>type</th><th>subject</th><th>conf</th><th>trust</th></tr></thead>
    <tbody>${rows || '<tr><td colspan="4" class="muted">no signals</td></tr>'}</tbody></table>
    ${quarantines ? `<p><b>quarantined</b></p><ul>${quarantines}</ul>` : ""}`;
}

async function loadDecisions() {
  const records = await api("/v1/decisions?limit=20");
  const rows = records
    .slice()
    .reverse()
    .map(
      (r) =>
        `<tr><td>${esc(r.requested_at.slice(11, 19))}</td><td>${esc(
          r.sku
        )}</td><td>${fmt(r.served_price)}</td><td>${esc(
          r.trigger
        )}</td><td>${esc(r.policy_arm)}</td>${
          r.degraded ? '<td class="degraded">yes</td>' : "<td>no</td>"
        }</tr>`
    )
    .join("");
  $("#decisions-out").innerHTML = `
    <table><thead><tr><th>time</th><th>sku</th><th>price</th><th>trigger</th><th>arm</th><th>degraded</th></tr></thead>
    <tbody>${rows || '<tr><td colspan="6" class="muted">no decisions yet</td></tr>'}</tbody></table>`;
}

// ------------------------------------------------------------------- wiring

const actions = {
  "load-status": loadStatus,
  "price-explain": priceExplain,
  "load-signals": loadSignals,
  "load-decisions": loadDecisions,
  review: (button) => submitReview(button.dataset.choice),
};

document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-action], .tab");
  if (!button) return;
  clearError();
  if (button.classList.contains("tab")) {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
    button.classList.add("active");
    $(`#tab-${button.dataset.tab}`).classList.add("active");
    return;
  }
  const handler = actions[button.dataset.action];
  if (!handler) return;
  button.disabled = true;
  try {
    await handler(button);
  } catch (error) {
    showError(error);
  } finally {
    button.disabled = false;
  }
});

for (const [id, key] of [["base", "pc.base"], ["key", "pc.key"]]) {
  const input = $(`#${id}`);
  input.value = state[id];
  input.addEventListener("change", () => {
    const value = input.value.trim();
    // Papercut guard: the API field is a URL, the Key field is the secret.
    // Putting the key here produced a confusing Tauri scope error — say it plainly.
    if (id === "base" && value && !/^https?:\/\/.+/.test(value)) {
      showError(
        new Error(
          `the API field expects a URL, not a key (got '${value.slice(0, 40)}')\n` +
            "Fix: set it to http://127.0.0.1:8000 and paste the key into the Key field"
        )
      );
      input.value = state.base; // restore the last good URL
      return;
    }
    state[id] = value;
    localStorage.setItem(key, value);
    if (id === "base" && value) loadStatus().catch((error) => showError(error));
  });
}

loadStatus().catch((error) => showError(error));
