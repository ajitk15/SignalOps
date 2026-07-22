// SignalOps v2 client shell.
// Phase 0b: login gate, sidebar navigation, live socket. The views themselves
// are placeholders until the phases that build them.

const el = (id) => document.getElementById(id);
const esc = (v) => String(v ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
  .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');

let principal = null;
let currentView = 'workflows';

// Operate is what you do; Configure is how it behaves. Keeping them apart is
// the whole point of the sidebar — an on-call user and someone tuning an agent
// are different people with different urgency.
const NAV = [
  { group: 'Operate', items: [
    { id: 'workflows', label: 'Workflows', phase: 3 },
    { id: 'runs', label: 'Runs', phase: 2 },
    { id: 'approvals', label: 'Approvals', phase: 2 },
  ] },
  { group: 'Configure', items: [
    { id: 'agents', label: 'Agents', phase: 1 },
    { id: 'connections', label: 'Connections', phase: 3 },
    { id: 'audit', label: 'Audit', phase: 0 },
  ] },
];

const VIEW_TITLES = Object.fromEntries(
  NAV.flatMap(g => g.items).map(i => [i.id, i.label]));

// --- authentication ---------------------------------------------------------

async function doLogin(event) {
  event.preventDefault();
  const status = el('login-status');
  status.textContent = 'Signing in…';
  try {
    const response = await fetch('/api/auth/login', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        display_name: el('login-name').value.trim(),
        role: el('login-role').value,
      }),
    });
    if (!response.ok) throw new Error((await response.json()).detail || 'sign-in failed');
    principal = await response.json();
    showApp();
  } catch (error) {
    status.textContent = String(error.message);
  }
}

async function doLogout() {
  await fetch('/api/auth/logout', { method: 'POST' });
  principal = null;
  el('app').classList.add('hidden');
  el('login-screen').classList.remove('hidden');
  el('login-status').textContent = '';
}

// --- shell ------------------------------------------------------------------

function renderNav() {
  el('nav').innerHTML = NAV.map(group => `
    <div class="nav-group">
      <div class="nav-group-label">${esc(group.group)}</div>
      ${group.items.map(item => `
        <button class="nav-item ${currentView === item.id ? 'on' : ''}"
                onclick="switchView('${item.id}')">${esc(item.label)}</button>`).join('')}
    </div>`).join('');
}

function renderWho() {
  if (!principal) return;
  el('who').innerHTML = `
    <div class="who-name">${esc(principal.display_name)}</div>
    <div class="who-role">${esc(principal.role)} · ${esc(principal.workspace.name)}</div>
    ${principal.identity_verified ? ''
      : '<div class="who-unverified" title="The login is a placeholder; this identity was not verified">unverified identity</div>'}`;
}

function renderKillswitch() {
  const pill = el('killswitch-pill');
  const on = principal?.workspace?.killswitch;
  pill.classList.toggle('hidden', !on);
  if (on) {
    pill.className = 'status-pill failing';
    pill.textContent = 'kill switch on — runs halted';
  }
}

function switchView(view) {
  currentView = view;
  el('view-title').textContent = VIEW_TITLES[view] || view;
  renderNav();
  if (view === 'audit') return renderAudit();
  if (view === 'agents') return renderAgents();
  renderPlaceholder(view);
}

// --- agent catalogue --------------------------------------------------------

let agentCache = [];

async function renderAgents() {
  el('view').innerHTML = '<div class="empty">Loading agents…</div>';
  try {
    const data = await (await fetch('/api/agents')).json();
    agentCache = data.agents;
  } catch {
    el('view').innerHTML = '<div class="empty">Agents could not be loaded.</div>';
    return;
  }
  const canEdit = principal.role === 'admin';
  el('view').innerHTML = `
    <div class="pipeline-note">
      Every agent that can run is listed here — the catalogue is the source of truth, not a
      summary of it. <strong>Tools and access tier are defined in code and cannot be changed
      from this screen</strong>; customisation covers the model, guidance and thresholds,
      which shape how an agent judges rather than what it can reach.
    </div>
    <div class="agent-grid">
      ${agentCache.map(agentCard).join('')}
    </div>
    ${canEdit ? '' : `<p class="empty">You are signed in as ${esc(principal.role)};
      customising agents requires admin.</p>`}`;
}

function agentCard(agent) {
  const canEdit = principal.role === 'admin';
  return `
    <div class="agent-item">
      <div class="agent-top">
        <h3>${esc(agent.name)}</h3>
        <span class="tier-badge tier-${esc(agent.tier)}">${esc(agent.tier.replace('_', ' '))}</span>
        ${agent.advisory_only
          ? '<span class="tier-badge">advisory</span>'
          : '<span class="tier-badge tier-write_external">drives an action</span>'}
        ${agent.customised ? '<span class="trigger-badge trigger-event">customised</span>' : ''}
        ${agent.enabled ? '' : '<span class="tier-badge">disabled</span>'}
      </div>
      <p class="agent-purpose">${esc(agent.purpose)}</p>
      <p class="agent-explain">${esc(agent.explanation)}</p>
      <div class="agent-meta">
        <span>model <code>${esc(agent.model)}</code></span>
        <span>tools ${agent.tools.length
          ? agent.tools.map(t => `<code>${esc(t)}</code>`).join(' ') : '<code>none</code>'}</span>
        ${agent.confidence_threshold != null
          ? `<span>gate at <code>${agent.confidence_threshold}</code></span>` : ''}
        <span>workflow <code>${esc(agent.workflow)}</code></span>
      </div>
      ${agent.enabled ? '' : `<div class="locked-note" style="border-color:var(--warn)">
        <strong>Disabled.</strong> ${esc(agent.disabled_effect)}</div>`}
      ${canEdit ? `<div class="row-actions" style="margin-top:12px">
        <button class="button" onclick="openAgentDialog('${esc(agent.id)}')">Edit</button>
        <button class="button ghost" onclick="toggleAgent('${esc(agent.id)}')">
          ${agent.enabled ? 'Disable' : 'Enable'}</button>
        <button class="button ghost" onclick="showPrompt('${esc(agent.id)}')">View prompt</button>
        ${agent.customised
          ? `<button class="button ghost" onclick="resetAgent('${esc(agent.id)}')">Reset</button>` : ''}
      </div>` : ''}
    </div>`;
}

// Quick toggle from the card. Disabling an agent the workflow depends on is
// allowed, but never silently — you are told what stops working first.
async function toggleAgent(id) {
  const agent = agentCache.find(a => a.id === id);
  const turningOff = agent.enabled;
  if (turningOff) {
    const warning = agent.optional
      ? `Disable ${agent.name}?\n\n${agent.disabled_effect}`
      : `${agent.name} is REQUIRED.\n\n${agent.disabled_effect}\n\nDisable anyway?`;
    if (!confirm(warning)) return;
  }
  const response = await fetch(`/api/agents/${id}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled: !agent.enabled }),
  });
  if (!response.ok) { toast('Could not change that agent'); return; }
  renderAgents();
}

async function showPrompt(id) {
  const data = await (await fetch(`/api/agents/${id}/prompt`)).json();
  el('view').insertAdjacentHTML('afterbegin', `
    <div class="pipeline-note" id="prompt-peek">
      <button class="dismiss" onclick="el('prompt-peek').remove()">✕</button>
      <strong>${esc(id)} — exact prompt</strong>
      <pre style="white-space:pre-wrap;font-size:11.5px;margin:9px 0 0">${esc(data.system_prompt)}</pre>
    </div>`);
}

function openAgentDialog(id) {
  const agent = agentCache.find(a => a.id === id);
  const body = `
    <p class="dialog-note">${esc(agent.purpose)}</p>

    <div class="field-row">
      <label class="switch">
        <input type="checkbox" id="ag-enabled" ${agent.enabled ? 'checked' : ''} />
        <span>Enabled</span>
      </label>
      <span class="field-hint">
        ${agent.optional
          ? 'Optional — the workflow still runs without it.'
          : '<strong>Required</strong> — the workflow cannot run without it.'}
      </span>
    </div>
    <p class="field-hint">If switched off: ${esc(agent.disabled_effect)}</p>

    <label for="ag-model">Model</label>
    <select id="ag-model" class="draft-field">
      ${agent.allowed_models.map(m =>
        `<option ${m === agent.model ? 'selected' : ''}>${esc(m)}</option>`).join('')}
    </select>
    <p class="field-hint">Default is <code>${esc(agent.default_model)}</code>.</p>

    ${agent.produces_confidence ? `
      <label for="ag-threshold">Confidence gate</label>
      <input id="ag-threshold" class="draft-field" type="number" min="0" max="1" step="0.05"
             value="${agent.confidence_threshold ?? ''}" />
      <p class="field-hint">Below this, the run stops and asks a human instead of proceeding.</p>
    ` : '<p class="field-hint">This agent does not produce a confidence score.</p>'}

    <div class="field-row">
      <label class="switch">
        <input type="checkbox" id="ag-approval" ${agent.requires_approval ? 'checked' : ''} />
        <span>Always require human approval</span>
      </label>
    </div>
    <p class="field-hint">
      ${agent.advisory_only
        ? 'This agent only advises, so approval is off by default.'
        : 'This agent’s output drives an action, so approval is on by default.'}
    </p>

    <label for="ag-guidance">Additional guidance</label>
    <textarea id="ag-guidance" class="draft-editor" style="min-height:110px"
      placeholder="Shape how this agent judges — domain conventions, what to prefer when the evidence is ambiguous.">${esc(agent.extra_guidance || '')}</textarea>
    <p class="field-hint">Appended below the safety rules as lower-authority text.</p>

    <p class="locked-note">
      <strong>Not editable by design:</strong> tools
      (<code>${agent.tools.join('</code> <code>') || 'none'}</code>) and access tier
      (<code>${esc(agent.tier)}</code>) are defined in code. Guidance that tries to override the
      safety rules is rejected — otherwise customising an agent would be a way around them.
    </p>

    <p>
      <button class="button" onclick="saveAgent('${esc(id)}')">Save changes</button>
      ${agent.customised
        ? `<button class="button ghost" onclick="closeDialog();resetAgent('${esc(id)}')">Reset to defaults</button>`
        : ''}
    </p>
    <p id="ag-status" class="dialog-note"></p>`;
  showDialog(`Edit ${agent.name}`, body);
}

async function saveAgent(id) {
  const agent = agentCache.find(a => a.id === id);
  const status = el('ag-status');
  const enabled = el('ag-enabled').checked;
  // Same honesty as the card toggle: turning off a required agent is allowed,
  // but you are told what it costs first.
  if (!enabled && agent.enabled && !agent.optional
      && !confirm(`${agent.name} is REQUIRED.\n\n${agent.disabled_effect}\n\nDisable anyway?`)) {
    return;
  }
  status.textContent = 'Saving…';
  const thresholdField = el('ag-threshold');
  const threshold = thresholdField ? thresholdField.value : '';
  try {
    const response = await fetch(`/api/agents/${id}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        enabled,
        model: el('ag-model').value,
        requires_approval: el('ag-approval').checked,
        extra_guidance: el('ag-guidance').value.trim() || null,
        confidence_threshold: threshold === '' ? null : Number(threshold),
      }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || 'save failed');
    closeDialog();
    renderAgents();
    toast(`${agent.name} updated`);
  } catch (error) {
    status.textContent = String(error.message);
  }
}

async function resetAgent(id) {
  if (!confirm(`Reset ${id} to its shipped defaults?`)) return;
  await fetch(`/api/agents/${id}/reset`, { method: 'POST' });
  renderAgents();
}

// --- feedback ---------------------------------------------------------------

let toastTimer;

function toast(message) {
  const node = el('toast');
  node.textContent = message;
  node.classList.add('on');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.remove('on'), 3200);
}

// --- lightweight dialog -----------------------------------------------------

function showDialog(title, bodyHtml) {
  closeDialog();
  document.body.insertAdjacentHTML('beforeend', `
    <dialog id="app-dialog">
      <button class="dialog-close" onclick="closeDialog()">Close</button>
      <h3>${esc(title)}</h3>
      ${bodyHtml}
    </dialog>`);
  el('app-dialog').showModal();
}

function closeDialog() {
  const dialog = el('app-dialog');
  if (dialog) { dialog.close(); dialog.remove(); }
}

function renderPlaceholder(view) {
  const item = NAV.flatMap(g => g.items).find(i => i.id === view);
  el('view').innerHTML = `
    <div class="pipeline-note">
      <strong>${esc(item.label)}</strong> is built in phase ${item.phase} of the rebuild.
      The shell, data model and permissions are in place; this view arrives with the
      functionality behind it rather than as an empty frame.
    </div>`;
}

async function renderAudit() {
  el('view').innerHTML = '<div class="audit-list" id="audit-list"><span class="empty">Loading…</span></div>';
  try {
    const data = await (await fetch('/api/audit?limit=100')).json();
    el('audit-list').innerHTML = data.entries.length ? `
      ${data.actor_verified ? '' : `<div class="pipeline-note">Actor names are
        <strong>self-asserted</strong> — the placeholder login does not verify identity.</div>`}
      ${data.entries.map(e => `
        <div class="audit-row">
          <span class="audit-actor">${esc(e.actor)}</span>
          <span>${esc(e.action.replace(/_/g, ' '))}</span>
          <span class="incident-id">${esc(e.entity_type)}</span>
          <span class="audit-time">${new Date(e.ts * 1000).toLocaleString()}</span>
        </div>`).join('')}`
      : '<span class="empty">No recorded actions yet.</span>';
  } catch {
    el('audit-list').innerHTML = '<span class="empty">Audit trail could not be loaded.</span>';
  }
}

function showApp() {
  el('login-screen').classList.add('hidden');
  el('app').classList.remove('hidden');
  renderWho();
  renderKillswitch();
  switchView(currentView);
  connect();
}

// --- live socket ------------------------------------------------------------

function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/ws/events`);
  ws.onopen = () => { el('conn').textContent = 'live'; el('conn').className = 'conn live'; };
  ws.onclose = () => {
    el('conn').textContent = 'reconnecting…';
    el('conn').className = 'conn down';
    setTimeout(connect, 2000);
  };
  ws.onerror = () => ws.close();
}

// --- boot -------------------------------------------------------------------

(async function boot() {
  try {
    const response = await fetch('/api/auth/me');
    if (response.ok) { principal = await response.json(); return showApp(); }
  } catch { /* fall through to the login gate */ }
  el('login-screen').classList.remove('hidden');
})();
