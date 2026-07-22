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
  renderPlaceholder(view);
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
