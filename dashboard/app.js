// SignalAIOps v2 client shell.
// Phase 0b: login gate, sidebar navigation, live socket. The views themselves
// are placeholders until the phases that build them.

const el = (id) => document.getElementById(id);
const esc = (v) => String(v ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
  .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');

let principal = null;
let currentView = 'home';
let passwordResetEmailConfigured = false;
let passwordResetToken = '';

const THEME_KEY = 'signalaiops-theme';

function currentTheme() {
  return document.documentElement.dataset.theme === 'light' ? 'light' : 'dark';
}

function syncThemeControls() {
  const theme = currentTheme();
  const next = theme === 'dark' ? 'light' : 'dark';
  document.querySelectorAll('[data-theme-toggle]').forEach(button => {
    button.setAttribute('aria-label', `Switch to ${next} mode`);
    button.title = `Switch to ${next} mode`;
    const icon = button.querySelector('[data-theme-icon]');
    const label = button.querySelector('[data-theme-label]');
    if (icon) icon.textContent = theme === 'dark' ? '☀' : '☾';
    if (label) label.textContent = theme === 'dark' ? 'Light' : 'Dark';
  });
  const themeColor = el('theme-color');
  if (themeColor) themeColor.content = theme === 'dark' ? '#0b1420' : '#f4f7f5';
}

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem(THEME_KEY, theme); } catch { /* preference remains in-memory */ }
  syncThemeControls();
}

function toggleTheme() {
  setTheme(currentTheme() === 'dark' ? 'light' : 'dark');
}

// Operate is what you do; Configure is how it behaves. Keeping them apart is
// the whole point of the sidebar — an on-call user and someone tuning an agent
// are different people with different urgency.
const NAV = [
  { group: 'Operate', items: [
    { id: 'home', label: 'Home', phase: 6 },
    { id: 'workflows', label: 'Workflows', phase: 2 },
    { id: 'runs', label: 'Runs', phase: 2 },
    { id: 'approvals', label: 'Approvals', phase: 2 },
  ] },
  { group: 'Configure', items: [
    { id: 'agents', label: 'Agents', phase: 1 },
    { id: 'connections', label: 'Connections', phase: 3 },
    { id: 'users', label: 'Users', phase: 6 },
    { id: 'audit', label: 'Audit', phase: 0 },
  ] },
];

const VIEW_TITLES = Object.fromEntries(
  NAV.flatMap(g => g.items).map(i => [i.id, i.label]));

// --- authentication ---------------------------------------------------------

async function loadAuthState() {
  try {
    const state = await (await fetch('/api/auth/state')).json();
    const note = el('bootstrap-note');
    note.classList.toggle('hidden', state.admin_configured);
    const requestButton = el('request-access-button');
    if (requestButton) {
      requestButton.classList.toggle('hidden', !state.registration_enabled);
    }
    passwordResetEmailConfigured = Boolean(state.password_reset_email_configured);
    const resetNote = el('password-reset-config-note');
    if (resetNote) resetNote.classList.toggle('hidden', passwordResetEmailConfigured);
    const resetSubmit = el('forgot-password-submit');
    if (resetSubmit) resetSubmit.disabled = !passwordResetEmailConfigured;
    if (!state.admin_configured) {
      note.innerHTML = '<strong>No administrator is configured.</strong> Set '
        + '<code>SIGNALOPS_ADMIN_EMAIL</code> and <code>SIGNALOPS_ADMIN_PASSWORD</code> '
        + 'where the server runs, then restart.';
    }
  } catch { /* the login form still works without this */ }
}

const AUTH_FORM_IDS = [
  'login-form',
  'registration-form',
  'forgot-password-form',
  'reset-password-form',
];

function showAuthForm(formId) {
  AUTH_FORM_IDS.forEach(id => {
    const form = el(id);
    if (form) form.classList.toggle('hidden', id !== formId);
  });
}

function showRegistrationForm() {
  showAuthForm('registration-form');
  el('registration-status').textContent = '';
  el('registration-name').focus();
}

function showLoginForm() {
  showAuthForm('login-form');
  passwordResetToken = '';
  if (window.location.hash.startsWith('#reset=')) {
    window.history.replaceState(null, '', `${window.location.pathname}${window.location.search}`);
  }
  el('login-email').focus();
}

function showForgotPasswordForm() {
  showAuthForm('forgot-password-form');
  const sourceEmail = el('login-email').value.trim();
  if (sourceEmail) el('forgot-password-email').value = sourceEmail;
  el('forgot-password-status').textContent = '';
  el('forgot-password-email').focus();
}

function showResetPasswordForm(token) {
  passwordResetToken = token;
  showAuthForm('reset-password-form');
  el('reset-password-status').textContent = '';
  el('reset-password-new').focus();
}

function resetTokenFromLocation() {
  if (!window.location.hash.startsWith('#reset=')) return '';
  try {
    return decodeURIComponent(window.location.hash.slice('#reset='.length));
  } catch {
    return '';
  }
}

async function submitForgotPassword(event) {
  event.preventDefault();
  const status = el('forgot-password-status');
  const submit = el('forgot-password-submit');
  if (!passwordResetEmailConfigured) {
    status.textContent = 'Email delivery is not configured for this environment.';
    status.className = 'dialog-note form-error';
    return;
  }
  submit.disabled = true;
  submit.setAttribute('aria-busy', 'true');
  status.textContent = 'Preparing a secure reset link…';
  status.className = 'dialog-note';
  try {
    const response = await fetch('/api/auth/forgot-password', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: el('forgot-password-email').value.trim() }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || 'could not request a reset link');
    status.textContent = result.message;
    status.className = 'dialog-note form-success';
  } catch (error) {
    status.textContent = String(error.message);
    status.className = 'dialog-note form-error';
  } finally {
    submit.disabled = !passwordResetEmailConfigured;
    submit.removeAttribute('aria-busy');
  }
}

async function submitPasswordReset(event) {
  event.preventDefault();
  const status = el('reset-password-status');
  const submit = el('reset-password-submit');
  const password = el('reset-password-new').value;
  if (!passwordResetToken) {
    status.textContent = 'This password-reset link is invalid or expired.';
    status.className = 'dialog-note form-error';
    return;
  }
  if (password !== el('reset-password-confirm').value) {
    status.textContent = 'The passwords do not match.';
    status.className = 'dialog-note form-error';
    return;
  }
  submit.disabled = true;
  submit.setAttribute('aria-busy', 'true');
  status.textContent = 'Setting your new password…';
  status.className = 'dialog-note';
  try {
    const response = await fetch('/api/auth/reset-password', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: passwordResetToken, new_password: password }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || 'could not reset the password');
    el('reset-password-new').value = '';
    el('reset-password-confirm').value = '';
    showLoginForm();
    const loginStatus = el('login-status');
    loginStatus.textContent = result.message;
    loginStatus.className = 'dialog-note form-success';
  } catch (error) {
    status.textContent = String(error.message);
    status.className = 'dialog-note form-error';
  } finally {
    submit.disabled = false;
    submit.removeAttribute('aria-busy');
  }
}

async function submitAccessRequest(event) {
  event.preventDefault();
  const status = el('registration-status');
  const submit = el('registration-submit');
  const password = el('registration-password').value;
  if (password !== el('registration-confirm').value) {
    status.textContent = 'The passwords do not match.';
    status.className = 'dialog-note form-error';
    return;
  }
  submit.disabled = true;
  submit.setAttribute('aria-busy', 'true');
  status.textContent = 'Submitting for administrator review…';
  status.className = 'dialog-note';
  try {
    const response = await fetch('/api/auth/register', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        display_name: el('registration-name').value.trim(),
        email: el('registration-email').value.trim(),
        password,
      }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || 'could not submit request');
    el('registration-password').value = '';
    el('registration-confirm').value = '';
    status.textContent = `${result.message} You can return to sign in.`;
    status.className = 'dialog-note form-success';
  } catch (error) {
    status.textContent = String(error.message);
    status.className = 'dialog-note form-error';
  } finally {
    submit.disabled = false;
    submit.removeAttribute('aria-busy');
  }
}

async function doLogin(event) {
  event.preventDefault();
  const status = el('login-status');
  const submit = el('login-submit');
  // Disable the button while the request is in flight so a double-tap or a
  // repeated Enter cannot fire a second sign-in.
  submit.disabled = true;
  submit.setAttribute('aria-busy', 'true');
  status.textContent = 'Signing in…';
  try {
    const response = await fetch('/api/auth/login', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: el('login-email').value.trim(),
                             password: el('login-password').value }),
    });
    if (!response.ok) throw new Error((await response.json()).detail || 'sign-in failed');
    principal = await response.json();
    el('login-password').value = '';
    showApp();
  } catch (error) {
    status.textContent = String(error.message);
  } finally {
    submit.disabled = false;
    submit.removeAttribute('aria-busy');
  }
}

async function doLogout() {
  await fetch('/api/auth/logout', { method: 'POST' });
  principal = null;
  el('app').classList.add('hidden');
  el('login-screen').classList.remove('hidden');
  el('login-status').textContent = '';
  showLoginForm();
  await loadAuthState();
}

function openPasswordDialog(forced = false) {
  showDialog(forced ? 'Set your password' : 'Change your password', `
    ${forced ? `<p class="dialog-note"><strong>You are using a password an administrator set
      for you.</strong> Choose your own before continuing — the rest of the app is locked
      until you do.</p>`
      : `<p class="dialog-note">Your current password is required even though you are signed
      in — an unattended browser is the common case, and re-asking is what stops it
      becoming an account takeover.</p>`}
    <label for="pw-current">${forced ? 'The password you signed in with' : 'Current password'}</label>
    <input id="pw-current" class="draft-field" type="password" autocomplete="current-password" />
    <label for="pw-new">New password</label>
    <input id="pw-new" class="draft-field" type="password" autocomplete="new-password" />
    <p class="field-hint">At least 10 characters.</p>
    <p class="row-actions">
      <button class="button" onclick="savePassword(${forced})">Set password</button>
      ${forced ? '' : '<button class="button ghost" onclick="closeDialog()">Cancel</button>'}</p>`);
  // A forced change must not be escapable via the close button or Escape.
  if (forced) {
    const dlg = el('app-dialog');
    const closeBtn = dlg && dlg.querySelector('.dialog-close');
    if (closeBtn) closeBtn.remove();
    if (dlg) dlg.addEventListener('cancel', (event) => event.preventDefault());
  }
}

async function savePassword(forced = false) {
  const response = await fetch('/api/auth/password', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ current_password: el('pw-current').value,
                           new_password: el('pw-new').value }),
  });
  if (!response.ok) return toast((await response.json()).detail || 'could not change', false);
  closeDialog();
  toast('Password changed');
  if (forced) {
    // The flag is now clear server-side; refresh identity and open the app.
    try { principal = await (await fetch('/api/auth/me')).json(); } catch { /* keep going */ }
    renderWho();
    switchView('home');
  }
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

const ROLES = ['viewer', 'operator', 'approver', 'admin'];

function renderWho() {
  if (!principal) return;
  el('who').innerHTML = `
    <div class="who-name">${esc(principal.display_name)}</div>
    <div class="who-role">${esc(principal.email)}</div>
    <div class="who-role-label">${esc(principal.role)}</div>
    ${principal.must_change_password
      ? '<div class="who-unverified">change your password</div>' : ''}
    <p class="row-actions">
      <button class="button ghost" onclick="openPasswordDialog()">Password</button>
      <button class="button ghost" onclick="doLogout()">Sign out</button>
    </p>`;
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

const VIEW_RENDERERS = {
  home: renderHome,
  audit: renderAudit,
  agents: renderAgents,
  workflows: renderWorkflows,
  runs: renderRuns,
  approvals: renderApprovals,
  connections: renderConnections,
  users: renderUsers,
};

// Bumped on every navigation. An async renderer captures the token it started
// under and checks it is still current before writing to the DOM, so a slow
// request that resolves after the user has moved on cannot paint the wrong view
// under the new title.
let viewToken = 0;

function viewIsCurrent(token) { return token === viewToken; }

function switchView(view) {
  viewToken += 1;
  currentView = view;
  el('view-title').textContent = VIEW_TITLES[view] || view;
  renderNav();
  closeNav();
  const render = VIEW_RENDERERS[view];
  return render ? render(viewToken) : renderPlaceholder(view);
}

// --- mobile navigation drawer -----------------------------------------------

function toggleNav() {
  el('app').classList.contains('nav-open') ? closeNav() : openNav();
}
function openNav() {
  el('app').classList.add('nav-open');
  el('nav-scrim').hidden = false;
  const toggle = el('nav-toggle');
  if (toggle) { toggle.setAttribute('aria-expanded', 'true'); toggle.setAttribute('aria-label', 'Close navigation'); }
}
function closeNav() {
  el('app').classList.remove('nav-open');
  const scrim = el('nav-scrim');
  if (scrim) scrim.hidden = true;
  const toggle = el('nav-toggle');
  if (toggle) { toggle.setAttribute('aria-expanded', 'false'); toggle.setAttribute('aria-label', 'Open navigation'); }
}

// --- onboarding wizard ------------------------------------------------------

// Six steps, and the fifth is the one that matters: Enable is not offered
// until a dry run has succeeded, so you always see what a workflow would do
// before it is able to do anything.
const WIZARD_STEPS = [
  { id: 'template', label: 'Template' },
  { id: 'connect', label: 'Connect' },
  { id: 'configure', label: 'Configure' },
  { id: 'agents', label: 'Review agents' },
  { id: 'dryrun', label: 'Dry run' },
  { id: 'enable', label: 'Enable' },
];

const wizard = { step: 0, workflowId: null, dismissed: false, active: false,
                 runId: null, template: 'incident_remediation' };

function startWizard() {
  Object.assign(wizard, { step: 0, workflowId: null, dismissed: false, active: true, runId: null });
  switchView('workflows');
}

function dismissWizard() {
  wizard.dismissed = true;
  wizard.active = false;
  renderWorkflows();
}

async function renderWizard(data) {
  const existing = data.workflows[0] || null;
  if (existing && !wizard.workflowId) wizard.workflowId = existing.id;
  const workflow = data.workflows.find(w => w.id === wizard.workflowId) || existing;
  const step = WIZARD_STEPS[wizard.step];
  el('view').innerHTML = `
    <div class="wizard">
      <ol class="wizard-steps">
        ${WIZARD_STEPS.map((s, i) => `
          <li class="wizard-step ${i === wizard.step ? 'on' : ''} ${i < wizard.step ? 'done' : ''}">
            <span class="wizard-num">${i + 1}</span>${esc(s.label)}</li>`).join('')}
      </ol>
      <div class="wizard-body" id="wizard-body"><div class="empty">Loading…</div></div>
    </div>
    <p class="row-actions">
      <button class="button ghost" onclick="dismissWizard()">Skip setup and see the list</button>
    </p>`;
  const render = {
    template: wizardTemplate, connect: wizardConnect, configure: wizardConfigure,
    agents: wizardAgents, dryrun: wizardDryRun, enable: wizardEnable,
  }[step.id];
  await render(workflow);
}

function wizardNav(backLabel, nextLabel, nextAction, { nextDisabled = false, note = '' } = {}) {
  return `
    <p class="row-actions">
      ${wizard.step > 0 ? `<button class="button ghost" onclick="wizardBack()">${
        esc(backLabel)}</button>` : ''}
      ${nextLabel ? `<button class="button" onclick="${nextAction}" ${
        nextDisabled ? 'disabled' : ''}>${esc(nextLabel)}</button>` : ''}
    </p>
    ${note ? `<p class="field-hint">${note}</p>` : ''}`;
}

function wizardBack() { wizard.step = Math.max(0, wizard.step - 1); renderWorkflows(); }
function wizardNext() { wizard.step = Math.min(WIZARD_STEPS.length - 1, wizard.step + 1); renderWorkflows(); }

const TEMPLATE_CARDS = [
  {
    id: 'incident_remediation',
    name: 'Incident remediation',
    what: 'Takes an incident from ServiceNow, gathers the recent changes and past incidents '
      + 'around it, forms a root-cause hypothesis, writes a proposed remediation plan back '
      + 'to the ticket, and asks a human before anything else.',
    touches: 'ServiceNow — reads incidents, changes and knowledge articles; appends a work '
      + 'note; can resolve a ticket.',
    unattended: 'Read, diagnose, and write a work note describing what it proposes.',
    never: 'Execute a remediation. It proposes; a person runs the steps and reports back.',
  },
  {
    id: 'ticket_to_pr',
    name: 'Ticket to pull request',
    what: 'Takes a bug, finds the relevant files, assesses how large and risky the change '
      + 'would be, writes it on a branch in a throwaway clone, runs your test suite, and '
      + 'asks a human to read the diff.',
    touches: 'Your repository — clones it, commits to a per-run branch, opens a draft pull '
      + 'request. Writes a note back to the ticket.',
    unattended: 'Read code, assess impact, write a change on a branch, and run your tests.',
    never: 'Merge anything, touch the default branch, or edit CI, infrastructure or secrets. '
      + 'A failing test suite blocks the pull request no matter what the reviewer agent says.',
  },
];

function wizardTemplate(workflow) {
  const chosen = (workflow && workflow.template) || wizard.template || 'incident_remediation';
  wizard.template = chosen;
  el('wizard-body').innerHTML = `
    <h3>What should this workflow do?</h3>
    ${TEMPLATE_CARDS.map(t => `
      <label class="template-card ${t.id === chosen ? 'chosen' : ''}">
        <input type="radio" name="wz-template" value="${t.id}" ${t.id === chosen ? 'checked' : ''}
          ${workflow ? 'disabled' : ''} onchange="pickTemplate('${t.id}')" />
        <strong>${esc(t.name)}</strong>
        <p class="agent-purpose">${esc(t.what)}</p>
        <ul class="template-facts">
          <li><strong>Touches:</strong> ${esc(t.touches)}</li>
          <li><strong>Will do unattended:</strong> ${esc(t.unattended)}</li>
          <li><strong>Will never do unattended:</strong> ${esc(t.never)}</li>
        </ul>
      </label>`).join('')}
    ${workflow ? '<p class="field-hint">The template is fixed once a workflow exists. '
      + 'Create another workflow to use the other one.</p>' : ''}
    <label for="wz-name">Name</label>
    <input id="wz-name" class="draft-field" value="${esc(workflow ? workflow.name
      : TEMPLATE_CARDS.find(t => t.id === chosen).name)}" />
    ${wizardNav('Back', workflow ? 'Next' : 'Create and continue', 'wizardCreate()')}`;
}

function pickTemplate(id) {
  wizard.template = id;
  renderWorkflows();
}

async function wizardCreate() {
  const name = el('wz-name').value.trim() || 'Incident remediation';
  if (!wizard.workflowId) {
    const response = await fetch('/api/workflows', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ template: wizard.template || 'incident_remediation',
                             name, dry_run: true }),
    });
    if (!response.ok) return toast((await response.json()).detail || 'could not create', false);
    wizard.workflowId = (await response.json()).id;
  }
  wizardNext();
}

async function wizardConnect() {
  const data = await (await fetch('/api/connections')).json();
  const rows = Object.entries(data.environment).map(([name, present]) => `
    <div class="audit-row">
      <span class="audit-actor"><code>${esc(name)}</code></span>
      <span class="tier-badge ${present ? 'ok' : 'bad'}">${present ? 'set' : 'missing'}</span>
    </div>`).join('');
  el('wizard-body').innerHTML = `
    <h3>Connect ServiceNow</h3>
    <p class="agent-purpose">SignalAIOps reads credentials from the environment and never
      stores them. There is no field here to type a password into — set these where the
      server runs, then test.</p>
    ${rows}
    <p class="field-hint">Reads need the instance URL and the read account. Writing work
      notes needs the write account as well; without it the workflow still runs and records
      what it would have written.</p>
    <p class="row-actions">
      <button class="button ghost" onclick="testEnvironmentConnection()">Test connection</button>
    </p>
    <p id="conn-result" class="field-hint"></p>
    ${wizardNav('Back', 'Next', 'wizardNext()', {
      note: data.missing_for_reads.length
        ? 'You can continue without credentials — the workflow will run on tickets you paste in.'
        : '' })}`;
}

async function testEnvironmentConnection() {
  const target = el('conn-result');
  target.textContent = 'Testing…';
  const result = await (await fetch('/api/connections/test', { method: 'POST' })).json();
  target.innerHTML = result.ok
    ? `<span class="tier-badge ok">connected</span> ${esc(result.detail)}${
        result.writes_available ? '' : ' Write credentials are not set, so work notes will be recorded rather than sent.'}`
    // The failure text explains what the status code can and cannot tell you,
    // so it is shown in full rather than summarised to "failed".
    : `<span class="tier-badge bad">failed</span> ${esc(result.detail)}`;
}

function wizardConfigure(workflow) {
  const config = (workflow && workflow.config) || {};
  const isCode = (workflow && workflow.template) === 'ticket_to_pr';
  el('wizard-body').innerHTML = `
    <h3>How should it run?</h3>
    ${isCode ? `
      <label for="wz-repo">Repository (clone URL)</label>
      <input id="wz-repo" class="draft-field" placeholder="https://github.com/acme/widget.git"
        value="${esc(config.repo_url || '')}" />
      <p class="field-hint">Configuration, never the ticket. A ticket that could name its own
        repository could choose what the bot writes to.</p>

      <label for="wz-fullname">Repository (owner/name, for the pull request)</label>
      <input id="wz-fullname" class="draft-field" placeholder="acme/widget"
        value="${esc(config.repo_full_name || '')}" />

      <label for="wz-base">Base branch</label>
      <input id="wz-base" class="draft-field" value="${esc(config.base_branch || 'main')}" />
      <p class="field-hint">The workflow commits to a per-run branch and opens a draft pull
        request. It cannot push to this branch and has no way to merge.</p>

      <label for="wz-tests">Test command</label>
      <input id="wz-tests" class="draft-field" placeholder="pytest -q"
        value="${esc(config.test_command || '')}" />
      <p class="field-hint">Run in the checkout after the change. <strong>A failing suite
        blocks the pull request</strong> regardless of what the reviewer agent thinks. Leave
        this empty and a run reports "not verified" rather than "passed".</p>

      <div class="field-row">
        <label class="switch"><input type="checkbox" id="wz-deps"
          ${config.allow_dependency_changes ? 'checked' : ''} /><span>Allow dependency
          changes</span></label>
        <span class="field-hint">Off by default. CI, infrastructure and secrets stay refused
          either way — there is no setting for those.</span>
      </div>
    ` : `
      <label for="wz-filter">Which incidents (ServiceNow encoded query)</label>
      <input id="wz-filter" class="draft-field" placeholder="active=true^priority<=2"
        value="${esc(config.filter_query || '')}" />
      <p class="field-hint">Leave empty to start runs by hand instead of polling.</p>

      <label for="wz-interval">Poll every (seconds)</label>
      <input id="wz-interval" class="draft-field" type="number" min="30" max="3600" step="30"
        value="${config.poll_interval_seconds || 120}" />
    `}

    <label for="wz-budget">Budget per run (USD)</label>
    <input id="wz-budget" class="draft-field" type="number" min="0.25" max="100" step="0.25"
      value="${config.run_budget_usd ?? 1}" />
    <p class="field-hint">A hard stop, not a warning. A run that reaches it is cancelled.</p>

    <div class="field-row">
      <label class="switch"><input type="checkbox" checked disabled /><span>Dry run</span></label>
      <span class="field-hint">Locked on until a dry run has succeeded. External writes are
        composed and recorded, and nothing leaves the process.</span>
    </div>
    ${wizardNav('Back', 'Save and continue', 'wizardSaveConfig()')}`;
}

async function wizardSaveConfig() {
  const response = await fetch(`/api/workflows/${wizard.workflowId}/config`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(el('wz-repo') ? {
      repo_url: el('wz-repo').value.trim(),
      repo_full_name: el('wz-fullname').value.trim(),
      base_branch: el('wz-base').value.trim() || 'main',
      test_command: el('wz-tests').value.trim(),
      allow_dependency_changes: el('wz-deps').checked,
      run_budget_usd: Number(el('wz-budget').value) || 1,
    } : {
      filter_query: el('wz-filter').value.trim(),
      poll_interval_seconds: Number(el('wz-interval').value) || 120,
      run_budget_usd: Number(el('wz-budget').value) || 1,
    }),
  });
  if (!response.ok) return toast((await response.json()).detail || 'could not save', false);
  wizardNext();
}

async function wizardAgents(workflow) {
  if (workflow) wizard.template = workflow.template;
  const data = await (await fetch('/api/agents')).json();
  const template = wizard.template || 'incident_remediation';
  const mine = data.agents.filter(a => [template, 'both'].includes(a.workflow));
  el('wizard-body').innerHTML = `
    <h3>These agents will run</h3>
    <p class="agent-purpose">Every agent that can act on your tickets, with the model it uses
      and how far it can reach. Nothing else runs — there are no hidden actors.</p>
    ${mine.map(a => `
      <div class="audit-row">
        <span class="audit-actor">${esc(a.name)}</span>
        <span class="tier-badge tier-${esc(a.tier)}">${esc(a.tier.replace('_', ' '))}</span>
        <span><code>${esc(a.model)}</code></span>
        <span class="dialog-note">${a.tools.length ? esc(a.tools.join(', ')) : 'no tools'}</span>
        ${a.enabled ? '' : '<span class="tier-badge bad">disabled</span>'}
      </div>`).join('')}
    <p class="field-hint">Change any of these later under Configure → Agents. Tools and tier
      are fixed in code and are not editable there.</p>
    ${wizardNav('Back', 'Next', 'wizardNext()')}`;
}

async function wizardDryRun(workflow) {
  const passed = workflow && workflow.dry_run_passed_at;
  el('wizard-body').innerHTML = `
    <h3>Try it once, writing nothing</h3>
    <p class="agent-purpose">Optional but worth doing: this runs the whole workflow against a
      real incident and composes the work note without sending it, so you see the output
      before anything is live.</p>
    ${passed ? `<p class="field-hint"><span class="tier-badge ok">passed</span>
      Dry run succeeded ${new Date(passed * 1000).toLocaleString()}.</p>` : ''}
    <label for="wz-ticket">Incident (JSON)</label>
    <textarea id="wz-ticket" class="draft-field prompt-field" rows="12">${
      esc(JSON.stringify({ ...SAMPLE_TICKET, number: 'INC' + Math.floor(Math.random() * 9e6 + 1e6) }, null, 2))}</textarea>
    <p class="row-actions">
      <button class="button" onclick="wizardStartDryRun()">Start the dry run</button>
    </p>
    <div id="wz-run-status"></div>
    ${wizardNav('Back', 'Next', 'wizardNext()', {
      note: passed ? '' : 'Optional. You can continue without it — the workflow starts in '
        + 'dry-run mode and every run still stops at the human gate.' })}`;
}

async function wizardStartDryRun() {
  let ticket;
  try {
    ticket = JSON.parse(el('wz-ticket').value);
  } catch (error) {
    return toast('That is not valid JSON: ' + error.message, false);
  }
  el('wz-run-status').innerHTML = '<p class="field-hint">Running…</p>';
  const response = await fetch(`/api/workflows/${wizard.workflowId}/runs`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ticket, dry_run: true }),
  });
  const body = await response.json();
  if (!response.ok) {
    const detail = body.detail;
    return toast((detail && detail.message) || detail || 'could not start', false);
  }
  wizard.runId = body.run_id;
  pollDryRun();
}

async function pollDryRun() {
  const run = await (await fetch(`/api/runs/${wizard.runId}`)).json();
  el('wz-run-status').innerHTML = `
    ${run.steps.map(s => `
      <div class="audit-row">
        <span class="audit-actor">${esc(s.node)}</span>
        <span class="tier-badge ${s.status === 'succeeded' ? 'ok'
          : s.status === 'failed' ? 'bad' : 'warn'}">${esc(s.status)}</span>
        ${s.error ? `<span>${esc(s.error)}</span>` : ''}
      </div>`).join('')}`;
  const done = ['succeeded', 'failed', 'cancelled', 'awaiting_approval'].includes(run.status);
  if (!done) return setTimeout(pollDryRun, 700);
  const wrote = run.steps.some(s => s.node === 'work_note' && s.status === 'succeeded');
  el('wz-run-status').insertAdjacentHTML('beforeend', wrote
    ? `<p class="field-hint"><span class="tier-badge ok">dry run passed</span>
       The work note below was composed and <strong>not sent</strong>.</p>
       <pre class="prompt-view">${esc(dryRunNote(run))}</pre>`
    : `<p class="field-hint"><span class="tier-badge bad">dry run failed</span>
       ${esc(run.error || 'the run did not reach the work note')}</p>`);
  // Unlock Next in place rather than re-rendering the step. Re-rendering would
  // wipe the work note that has just been printed, which is the one thing this
  // step exists to show you.
  if (wrote) {
    const next = [...document.querySelectorAll('.wizard-body button')]
      .find(b => b.textContent.trim() === 'Next');
    if (next) next.disabled = false;
  }
}

function dryRunNote(run) {
  const step = run.steps.find(s => s.node === 'work_note');
  return (step && step.output && JSON.stringify(step.output, null, 2)) || '';
}

async function wizardEnable(workflow) {
  const canEnable = workflow && workflow.tested;
  el('wizard-body').innerHTML = `
    <h3>Turn it on</h3>
    <p class="agent-purpose">Enabling lets the workflow run on its own. It still proposes
      rather than acts, and it still pauses for a human before anything is handed over.</p>
    ${canEnable ? '' : `<div class="pipeline-note">You have not run a test yet. Enabling is
      allowed anyway — the workflow starts in dry-run mode and every run stops at the human
      gate — but you will be seeing its output for the first time on a real ticket.</div>`}
    <div class="field-row">
      <label class="switch"><input type="checkbox" id="wz-poll" ${
        workflow && workflow.polling ? 'checked' : ''} /><span>Poll ServiceNow automatically</span></label>
      <span class="field-hint">Off means runs are started by hand.</span>
    </div>
    <p class="row-actions">
      <button class="button ghost" onclick="wizardBack()">Back</button>
      <button class="button" onclick="wizardDoEnable()">Enable workflow</button>
    </p>`;
}

async function wizardDoEnable() {
  const response = await fetch(`/api/workflows/${wizard.workflowId}/enable`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled: true, poll_enabled: el('wz-poll').checked }),
  });
  if (!response.ok) return toast((await response.json()).detail || 'could not enable', false);
  wizard.dismissed = true;
  wizard.active = false;
  toast('Workflow enabled');
  renderWorkflows();
}

// --- home dashboard ---------------------------------------------------------

function greeting() {
  const h = new Date().getHours();
  return h < 12 ? 'Good morning' : h < 18 ? 'Good afternoon' : 'Good evening';
}

const RUN_STATUS_LABEL = {
  succeeded: 'Succeeded', failed: 'Failed', cancelled: 'Cancelled',
  awaiting_approval: 'Awaiting approval', running: 'Running', pending: 'Pending',
};

async function renderHome(token = viewToken) {
  el('view').innerHTML = '<div class="empty">Loading…</div>';
  let d;
  try {
    d = await (await fetch('/api/overview')).json();
  } catch {
    el('view').innerHTML = '<div class="empty">The dashboard could not be loaded.</div>';
    return;
  }
  if (!viewIsCurrent(token)) return;
  const c = d.counts;

  // A brand-new workspace gets a genuine welcome and one clear next step
  // rather than a wall of zeroes.
  if (d.needs_onboarding && c.workflows_total === 0) {
    el('view').innerHTML = `
      <div class="home-welcome">
        <h2>${greeting()}, ${esc(d.greeting_name)}.</h2>
        <p>Welcome to SignalAIOps. It watches a ServiceNow queue, diagnoses the incidents
          that land there, and proposes fixes for you to approve — nothing acts on its own.</p>
        <p class="home-steps-label">Two steps to your first workflow:</p>
        <ol class="home-steps">
          <li><strong>Connect ServiceNow.</strong> Point it at an instance and a queue.</li>
          <li><strong>Create a workflow.</strong> Pick a template, review the agents, try it once.</li>
        </ol>
        <p class="row-actions">
          <button class="button" onclick="switchView('connections')">Add a connection</button>
          <button class="button ghost" onclick="switchView('workflows')">Create a workflow</button>
        </p>
        ${d.simulated ? homeSimBanner() : ''}
      </div>`;
    return;
  }

  const tiles = [
    { label: 'Enabled workflows', value: c.workflows_enabled,
      sub: `${c.workflows_polling} polling`, view: 'workflows' },
    { label: 'Pending approvals', value: c.pending_approvals,
      sub: c.pending_approvals ? 'need a human' : 'all clear',
      tone: c.pending_approvals ? 'warn' : 'ok', view: 'approvals' },
    { label: 'Runs today', value: c.runs_today,
      sub: `${c.runs_total} all time`, view: 'runs' },
    { label: 'Spend today', value: '$' + (c.spend_today_usd || 0).toFixed(2),
      sub: c.running ? `${c.running} running now` : 'no active runs', view: 'runs' },
  ];

  el('view').innerHTML = `
    <div class="home-head">
      <h2>${greeting()}, ${esc(d.greeting_name)}.</h2>
      <span class="home-role">${esc(d.role)}</span>
    </div>
    ${d.killswitch ? `<div class="home-alert bad">The workspace kill switch is on — every run
      is halted. An admin can lift it from the header.</div>` : ''}
    ${d.simulated ? homeSimBanner() : ''}

    <div class="home-tiles">
      ${tiles.map(t => `
        <button class="home-tile" onclick="switchView('${t.view}')">
          <span class="home-tile-label">${esc(t.label)}</span>
          <span class="home-tile-value ${t.tone || ''}">${esc(String(t.value))}</span>
          <span class="home-tile-sub">${esc(t.sub)}</span>
        </button>`).join('')}
    </div>

    ${c.pending_approvals ? `<div class="home-callout clickable" role="button" tabindex="0" data-activate
      aria-label="${c.pending_approvals} approval${c.pending_approvals > 1 ? 's' : ''} waiting; go to approvals"
      onclick="switchView('approvals')">
      <i class="ti">⚠</i>
      <span><strong>${c.pending_approvals} approval${c.pending_approvals > 1 ? 's' : ''}</strong>
        waiting on a human. Review ${c.pending_approvals > 1 ? 'them' : 'it'} →</span>
    </div>` : ''}

    <div class="home-cols">
      <div class="home-panel">
        <div class="home-panel-head">Recent runs
          <button class="link-button" onclick="switchView('runs')">All runs</button></div>
        ${d.recent_runs.length ? d.recent_runs.map(homeRunRow).join('')
          : '<div class="home-panel-empty">No runs yet.</div>'}
      </div>
      <div class="home-panel">
        <div class="home-panel-head">Connections
          <button class="link-button" onclick="switchView('connections')">Manage</button></div>
        ${d.connection_health.length ? d.connection_health.map(homeConnRow).join('')
          : '<div class="home-panel-empty">None configured.</div>'}
      </div>
    </div>`;
}

function homeSimBanner() {
  return `<div class="home-alert">Running in <strong>simulated mode</strong> — no
    <code>ANTHROPIC_API_KEY</code> is set, so agent output is placeholder text and every
    result is labelled simulated. Runs still execute end to end.</div>`;
}

function homeRunRow(r) {
  const status = RUN_STATUS_CLASS[r.status] || '';
  return `
    <div class="home-run clickable" role="button" tabindex="0" data-activate
      aria-label="Run ${esc(r.trigger_ref || r.id.slice(0, 8))}, ${
        esc(RUN_STATUS_LABEL[r.status] || r.status)}" onclick="showRun('${r.id}')">
      <span class="home-run-ref">${esc(r.trigger_ref || r.id.slice(0, 8))}</span>
      <span class="tier-badge ${status}">${esc(RUN_STATUS_LABEL[r.status] || r.status)}</span>
      <span class="home-run-time">${new Date(r.started_at * 1000).toLocaleTimeString([],
        { hour: '2-digit', minute: '2-digit' })}</span>
    </div>`;
}

function homeConnRow(c) {
  const dot = c.last_test_ok === true ? 'on' : c.last_test_ok === false ? 'off' : 'unknown';
  const word = c.last_test_ok === true ? 'Connected'
    : c.last_test_ok === false ? 'Failing' : 'Untested';
  return `
    <div class="home-conn">
      <span class="conn-logo">${CONNECTOR_LOGOS[c.kind] || ''}</span>
      <span class="home-conn-name">${esc(c.name)}</span>
      <span class="conn-status ${dot}"><span class="conn-status-dot"></span>${word}</span>
    </div>`;
}

// --- workflows --------------------------------------------------------------

const SAMPLE_TICKET = {
  number: 'INC0012345',
  short_description: 'IBM MQ channel SYSTEM.ADMIN.SVRCONN in RETRYING on QM1',
  description: 'Channel retrying for 20 minutes. Applications report 2059 connection refused. '
    + 'Queue depth on APP.REQUEST is at 8200 of 10000.',
  configuration_item: 'QM1',
  recent_changes: ['CHG0004411 — firewall rule update on the QM1 subnet, 08:55Z'],
  past_incidents: ['INC0011980 — same channel RETRYING after a firewall change'],
  kb_articles: ['KB0010023 — Diagnosing MQ 2059 errors'],
};

async function renderWorkflows(token = viewToken) {
  el('view').innerHTML = '<div class="empty">Loading workflows…</div>';
  let data;
  try {
    data = await (await fetch('/api/workflows')).json();
  } catch {
    el('view').innerHTML = '<div class="empty">Workflows could not be loaded.</div>';
    return;
  }
  if (!viewIsCurrent(token)) return;
  // A fresh workspace lands on the wizard, not on an empty list — an empty
  // dashboard tells you nothing about what to do next. `active` is separate
  // from that so Set up can re-enter the wizard on a workflow that is already
  // enabled, which is exactly when someone wants to change its filter.
  if (wizard.active || (!data.workflows.some(w => w.enabled) && !wizard.dismissed)) {
    return renderWizard(data);
  }
  const isAdmin = principal.role === 'admin';
  const canRun = ['operator', 'approver', 'admin'].includes(principal.role);
  el('view').innerHTML = `
    <div class="pipeline-note">
      A workflow is a template with configuration, not a free-form graph. Start a run to
      watch it execute; it pauses at the human gate before anything is proposed for action.
      <strong>Download</strong> gives you the whole thing as a runnable Python app —
      graph, agents, requirements and a Dockerfile.
    </div>
    <div class="row-actions">
      <button class="button" onclick="openWorkflowDialog()" ${isAdmin ? '' : 'disabled'}
        title="${isAdmin ? 'Create a workflow' : 'Creating workflows requires the admin role'}">
        New workflow</button>
    </div>
    ${data.workflows.length ? data.workflows.map(w => workflowCard(w, canRun)).join('') : `
      <div class="empty">No workflows yet.${isAdmin ? ' Create one to get started.'
        : ' An admin needs to create one.'}</div>`}`;
}

function workflowCard(w, canRun) {
  return `
    <div class="wf-card">
      <div class="wf-main">
        <div class="wf-title">
          <strong>${esc(w.name)}</strong>
          <span class="tier-badge ${w.enabled ? 'ok' : ''}">${w.enabled ? 'enabled' : 'off'}</span>
          ${w.polling ? '<span class="tier-badge warn">polling</span>' : ''}
        </div>
        <div class="wf-meta">
          <span>${esc(w.template.replace(/_/g, ' '))}</span>
          <span>·</span>
          <span>${w.config.dry_run ? 'dry run' : 'live writes'}</span>
          <span>·</span>
          <span>$${(w.config.run_budget_usd ?? 1).toFixed(2)}/run</span>
          ${w.tested ? '' : '<span>·</span><span class="wf-warn">never tested</span>'}
        </div>
      </div>
      <div class="wf-actions">
        <button class="button" onclick="openRunDialog('${w.id}')" ${canRun ? '' : 'disabled'}
          title="${canRun ? 'Start a run' : 'Starting a run requires the operator role'}">
          Run</button>
        <button class="button ghost small" onclick="resumeWizard('${w.id}')">Set up</button>
        <button class="button ghost small" onclick="exportWorkflow('${w.id}')"
          ${w.exportable ? '' : 'disabled'} title="Download as a standalone Python app">
          Download</button>
      </div>
    </div>`;
}

function exportWorkflow(id) { window.location = `/api/workflows/${id}/export`; }

function resumeWizard(workflowId) {
  Object.assign(wizard, { step: 1, workflowId, dismissed: false, active: true });
  renderWorkflows();
}

function openWorkflowDialog() {
  showDialog('New workflow', `
      <label for="wf-template">Template</label>
      <select id="wf-template" class="draft-field">
        <option value="incident_remediation">Incident remediation</option>
      </select>
      <label for="wf-name">Name</label>
      <input id="wf-name" class="draft-field" value="Incident remediation" />
      <label for="wf-budget">Budget per run (USD)</label>
      <input id="wf-budget" class="draft-field" type="number" step="0.25" min="0.25" value="1.00" />
      <label class="switch"><input type="checkbox" id="wf-dryrun" checked /><span>Dry run — record what it would write externally, send nothing</span></label>
      <p class="dialog-note">Leave dry run on until you have watched a run end to end.</p>
      <p class="row-actions"><button class="button" onclick="saveWorkflow()">Create</button>
        <button class="button ghost" onclick="closeDialog()">Cancel</button></p>`);
}

async function saveWorkflow() {
  const response = await fetch('/api/workflows', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      template: el('wf-template').value,
      name: el('wf-name').value.trim() || 'Incident remediation',
      dry_run: el('wf-dryrun').checked,
      run_budget_usd: Number(el('wf-budget').value) || 1,
    }),
  });
  if (!response.ok) return toast((await response.json()).detail || 'could not create', false);
  closeDialog();
  toast('Workflow created');
  renderWorkflows();
}

function openRunDialog(workflowId) {
  showDialog('Start a run', `
      <p class="dialog-note">The ticket below is <strong>data</strong>. It is fenced in the
        prompt as untrusted input, so instructions written inside it are reported rather
        than followed.</p>
      <label for="run-ticket">Incident (JSON)</label>
      <textarea id="run-ticket" class="draft-field prompt-field" rows="14">${
        esc(JSON.stringify(SAMPLE_TICKET, null, 2))}</textarea>
      <p class="row-actions">
        <button class="button" onclick="startRun('${workflowId}')">Start run</button>
        <button class="button ghost" onclick="closeDialog()">Cancel</button></p>`);
}

async function startRun(workflowId) {
  let ticket;
  try {
    ticket = JSON.parse(el('run-ticket').value);
  } catch (error) {
    return toast('That is not valid JSON: ' + error.message, false);
  }
  const response = await fetch(`/api/workflows/${workflowId}/runs`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ticket }),
  });
  const body = await response.json();
  if (!response.ok) {
    // A duplicate ticket comes back as a structured detail with the run that
    // already exists; everything else is a plain string.
    const detail = body.detail;
    if (detail && detail.run_id) {
      closeDialog();
      toast(detail.message, false);
      return switchView('runs');
    }
    return toast((typeof detail === 'string' && detail) || 'could not start the run', false);
  }
  closeDialog();
  toast('Run started');
  switchView('runs');
}

// --- runs -------------------------------------------------------------------

const RUN_STATUS_CLASS = {
  succeeded: 'ok', failed: 'bad', cancelled: 'bad',
  awaiting_approval: 'warn', running: 'warn', pending: 'warn',
};

async function renderRuns(token = viewToken) {
  el('view').innerHTML = '<div class="empty">Loading runs…</div>';
  let data;
  try {
    data = await (await fetch('/api/runs')).json();
  } catch {
    el('view').innerHTML = '<div class="empty">Runs could not be loaded.</div>';
    return;
  }
  if (!viewIsCurrent(token)) return;
  if (!data.runs.length) {
    el('view').innerHTML = '<div class="empty">No runs yet. Start one from Workflows.</div>';
    return;
  }
  const active = data.runs.filter(
    r => ['running', 'awaiting_approval', 'pending'].includes(r.status)).length;
  el('view').innerHTML = `
    <div class="table-head">
      <span class="table-count">${data.runs.length} runs · ${active} active</span>
    </div>
    <div class="table-scroll">
    <table class="data-table">
      <thead><tr>
        <th class="col-status">Status</th><th>Incident</th><th>Mode</th>
        <th>Started</th><th class="num">Duration</th><th class="num">Cost</th><th></th>
      </tr></thead>
      <tbody>${data.runs.map(runRow).join('')}</tbody>
    </table>
    </div>`;
}

function _runDuration(run) {
  if (!run.finished_at) return run.status === 'awaiting_approval' ? 'paused' : '—';
  const s = Math.max(0, run.finished_at - run.started_at);
  return s < 60 ? `${s.toFixed(1)}s` : `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
}

function runRow(run) {
  const status = RUN_STATUS_CLASS[run.status] || '';
  const when = new Date(run.started_at * 1000);
  const today = when.toDateString() === new Date().toDateString();
  return `
    <tr class="clickable" role="button" tabindex="0" data-activate onclick="showRun('${run.id}')">
      <td class="col-status"><span class="tier-badge ${status}">${
        esc(RUN_STATUS_LABEL[run.status] || run.status)}</span></td>
      <td class="incident-id">${esc(run.trigger_ref || run.id.slice(0, 8))}${
        run.error ? ` <span class="row-flag bad" title="${esc(run.error)}">error</span>` : ''}</td>
      <td class="muted">${run.dry_run ? 'dry run' : 'live'}</td>
      <td class="muted">${today
        ? when.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
        : when.toLocaleDateString()}</td>
      <td class="num muted">${_runDuration(run)}</td>
      <td class="num muted">$${(run.cost_usd || 0).toFixed(4)}</td>
      <td class="col-action"><button class="button ghost small"
        onclick="event.stopPropagation(); showRun('${run.id}')">Timeline</button></td>
    </tr>`;
}

async function showRun(runId) {
  const run = await (await fetch(`/api/runs/${runId}`)).json();
  showDialog(`Run ${run.trigger_ref || runId.slice(0, 8)}`, `
      <p class="dialog-note">${esc(run.status.replace(/_/g, ' '))} ·
        $${(run.cost_usd || 0).toFixed(4)} · ${run.dry_run ? 'dry run' : 'live'}</p>
      ${run.steps.map(step => `
        <div class="audit-row">
          <span class="audit-actor">${esc(step.node)}</span>
          <span class="tier-badge ${step.status === 'succeeded' ? 'ok' : 'bad'}">${
            esc(step.status)}</span>
          <span>${step.agent_id ? esc(step.agent_id) : '<em>deterministic</em>'}</span>
          <span class="audit-time">${step.finished_at
            ? ((step.finished_at - step.started_at).toFixed(2) + 's') : '…'}</span>
        </div>
        ${step.error ? `<p class="agent-purpose">${esc(step.error)}</p>` : ''}
        ${step.output ? `<pre class="prompt-view">${
          esc(JSON.stringify(step.output, null, 2))}</pre>` : ''}`).join('')}`);
}

// --- approvals --------------------------------------------------------------

async function renderApprovals(token = viewToken) {
  el('view').innerHTML = '<div class="empty">Loading approvals…</div>';
  let data;
  try {
    data = await (await fetch('/api/approvals')).json();
  } catch {
    el('view').innerHTML = '<div class="empty">Approvals could not be loaded.</div>';
    return;
  }
  if (!viewIsCurrent(token)) return;
  el('view').innerHTML = `
    <div class="table-head">
      <span class="table-count">${data.approvals.length} waiting on a human</span>
    </div>
    ${data.can_decide ? '' : `<div class="pipeline-note">You are signed in as
      <strong>${esc(principal.role)}</strong> and can see this queue but not act on it.
      An admin can change your role under Users.</div>`}
    ${data.approvals.length ? data.approvals.map(a => approvalCard(a, data.can_decide)).join('')
      : '<div class="empty">Nothing is waiting on a human.</div>'}`;
}

// The two gates ask different questions, so they get different buttons.
// Labelling "did running it work?" as Approve/Reject would invite someone to
// approve a plan they never ran, which is the one thing that can resolve a
// ticket that is not actually fixed.
const GATE_COPY = {
  gate: { yes: 'Approve', no: 'Reject', ask: 'Approve this plan',
          notePrompt: { yes: 'Note (optional)', no: 'Why are you rejecting it?' } },
  hand_off: { yes: 'It worked', no: 'It did not resolve it',
              ask: 'Report what happened when you ran it',
              notePrompt: { yes: 'What did you do? (optional)',
                            no: 'What happened?' } },
};

const RISK_TONE = { low: 'ok', medium: 'warn', high: 'bad' };

function approvalCard(approval, canDecide) {
  const copy = GATE_COPY[approval.node] || GATE_COPY.gate;
  const payload = approval.payload || {};
  const plan = payload.plan || {};
  const diagnosis = payload.diagnosis || {};
  const steps = plan.steps || [];
  const awaitingReport = approval.node === 'hand_off';
  const confidence = diagnosis.confidence != null ? Math.round(diagnosis.confidence * 100) : null;
  return `
    <div class="appr-card">
      <div class="appr-head">
        <strong>${esc(approval.summary)}</strong>
        <div class="appr-chips">
          ${plan.risk ? `<span class="tier-badge ${RISK_TONE[plan.risk] || ''}">${
            esc(plan.risk)} risk</span>` : ''}
          ${confidence != null ? `<span class="tier-badge">${confidence}% confident</span>` : ''}
          ${steps.length ? `<span class="tier-badge">${steps.length} step${
            steps.length > 1 ? 's' : ''}</span>` : ''}
          ${plan.requires_downtime ? '<span class="tier-badge bad">downtime</span>' : ''}
          ${awaitingReport ? '<span class="tier-badge warn">awaiting report</span>' : ''}
          ${payload.simulated ? '<span class="tier-badge warn">simulated</span>' : ''}
        </div>
      </div>
      ${diagnosis.root_cause ? `<p class="appr-cause"><span class="appr-label">Cause</span>
        ${esc(diagnosis.root_cause)}</p>` : ''}
      ${awaitingReport ? `<p class="appr-note">Approved — run the steps, then report the
        outcome. The ticket is resolved only if you confirm success.</p>` : ''}
      ${steps.length ? `<ol class="appr-steps">
        ${steps.map(step => `<li>
          <span class="appr-step-action">${esc(step.action || '')}</span>
          ${step.verify ? `<span class="appr-step-aside">verify: ${esc(step.verify)}</span>` : ''}
          ${step.rollback ? `<span class="appr-step-aside">rollback: ${esc(step.rollback)}</span>` : ''}
        </li>`).join('')}
      </ol>` : ''}
      <div class="appr-foot">
        <span class="appr-hash" title="Your approval is bound to this exact plan">🔒 ${
          esc(approval.payload_hash.slice(0, 10))}…</span>
        <div class="appr-actions">
          <button class="button" onclick="decide('${approval.id}', true, '${approval.node}')"
            ${canDecide ? '' : 'disabled'}
            title="${canDecide ? esc(copy.ask) : 'Deciding requires the approver role'}">
            ${esc(copy.yes)}</button>
          <button class="button ghost" onclick="decide('${approval.id}', false, '${approval.node}')"
            ${canDecide ? '' : 'disabled'}>${esc(copy.no)}</button>
        </div>
      </div>
    </div>`;
}

async function decide(approvalId, approved, node) {
  const copy = GATE_COPY[node] || GATE_COPY.gate;
  const note = window.prompt(approved ? copy.notePrompt.yes : copy.notePrompt.no) ?? '';
  const response = await fetch(`/api/approvals/${approvalId}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ approved, note: note || null }),
  });
  if (!response.ok) return toast((await response.json()).detail || 'could not record', false);
  toast(approved ? 'Approved — the run has resumed' : 'Rejected');
  renderApprovals();
}

// --- agent catalogue --------------------------------------------------------

let agentCache = [];
let agentsTab = 'incident_remediation';

let customAgents = [];
let grantableTools = [];
let canAuthorAgents = false;
let canApproveAgents = false;

async function renderAgents(token = viewToken) {
  el('view').innerHTML = '<div class="empty">Loading agents…</div>';
  try {
    const data = await (await fetch('/api/agents')).json();
    agentCache = data.agents;
    customAgents = data.custom_agents || [];
    grantableTools = data.grantable_tools || [];
    canAuthorAgents = !!data.can_author;
    canApproveAgents = !!data.can_approve;
  } catch {
    el('view').innerHTML = '<div class="empty">Agents could not be loaded.</div>';
    return;
  }
  if (!viewIsCurrent(token)) return;
  const canEdit = principal.role === 'admin';
  const tabs = [
    { id: 'incident_remediation', label: 'Incident remediation' },
    { id: 'ticket_to_pr', label: 'Ticket → PR' },
  ];
  const inTab = (a, t) => a.workflow === t || a.workflow === 'both';
  if (!tabs.some(t => t.id === agentsTab)) agentsTab = tabs[0].id;
  const builtins = agentCache.filter(a => inTab(a, agentsTab));
  // Approved custom agents run alongside the built-ins, so they sit in the same
  // grid under the tab they belong to. Pending and rejected ones go to a review
  // panel — they exist but cannot run yet.
  const approvedCustom = customAgents.filter(c => c.status === 'approved' && inTab(c, agentsTab));
  const pending = customAgents.filter(c => c.status === 'pending_review');
  const rejected = customAgents.filter(c => c.status === 'rejected');

  el('view').innerHTML = `
    <div class="pipeline-note">
      Every agent that can run is listed here — the catalogue is the source of truth.
      Change the model, rewrite the prompt, set thresholds, enable or disable. The seven
      built-in agents have <strong>tools and tier fixed in code</strong>. You can also
      <strong>author your own</strong> — those pick from a grantable tool set (never a shell),
      their tier is derived, and a non-admin's agent runs only after an admin approves it.
    </div>
    ${canEdit ? '' : `<div class="pipeline-note" style="border-color:var(--warn)">
      <strong>You are acting as ${esc(principal.role)}.</strong> You can propose an agent;
      an admin approves it before it runs. Editing built-in agents is an admin action.</div>`}

    ${pending.length ? `<div class="review-panel">
      <div class="review-head">${pending.length} agent${pending.length > 1 ? 's' : ''}
        awaiting review${canApproveAgents ? '' : ' (an admin decides)'}</div>
      ${pending.map(c => customAgentCard(c, true)).join('')}
    </div>` : ''}

    <div class="agent-tabbar">
      <div class="agent-tabs" role="tablist" aria-label="Agents by workflow">
        ${tabs.map(t => {
          const count = agentCache.filter(a => inTab(a, t.id)).length
            + customAgents.filter(c => c.status === 'approved' && inTab(c, t.id)).length;
          const on = t.id === agentsTab;
          return `<button class="agent-tab ${on ? 'on' : ''}" role="tab"
            id="agent-tab-${t.id}" aria-controls="agent-panel" aria-selected="${on}"
            tabindex="${on ? '0' : '-1'}" onclick="selectAgentsTab('${t.id}')"
            onkeydown="handleAgentTabKey(event, '${t.id}')">${esc(t.label)}
            <span class="agent-tab-count">${count}</span></button>`;
        }).join('')}
      </div>
      <div class="row-actions">
        ${canAuthorAgents ? `<button class="button small" onclick="openCustomAgentDialog()">
          New agent</button>` : ''}
        <button class="button ghost small" onclick="exportAllAgents()">Download all (.zip)</button>
      </div>
    </div>
    <div class="agent-grid" id="agent-panel" role="tabpanel"
      aria-labelledby="agent-tab-${agentsTab}">
      ${builtins.map(agentCard).join('')}
      ${approvedCustom.map(c => customAgentCard(c, false)).join('')}
    </div>
    ${rejected.length ? `<div class="review-panel muted">
      <div class="review-head">Rejected</div>
      ${rejected.map(c => customAgentCard(c, false)).join('')}
    </div>` : ''}`;
}

function customAgentCard(c, inReview) {
  const mine = c.created_by === principal.id;
  const canEdit = canApproveAgents || (mine && c.status === 'pending_review');
  return `
    <div class="agent-item custom-agent">
      <div class="agent-top">
        <h3>${esc(c.name)}</h3>
        <span class="tier-badge tier-${esc(c.tier)}">${esc(c.tier.replace('_', ' '))}</span>
        <span class="trigger-badge trigger-event">custom</span>
        ${c.status === 'approved' ? '<span class="tier-badge ok">approved</span>'
          : c.status === 'pending_review' ? '<span class="tier-badge warn">pending review</span>'
          : '<span class="tier-badge bad">rejected</span>'}
        ${c.enabled ? '' : '<span class="tier-badge">disabled</span>'}
      </div>
      <p class="agent-purpose">${esc(c.purpose)}</p>
      ${c.explanation ? `<p class="agent-explain">${esc(c.explanation)}</p>` : ''}
      <div class="agent-meta">
        <span>model <code>${esc(c.model)}</code></span>
        <span>tools ${(c.tools || []).length
          ? c.tools.map(t => `<code>${esc(t)}</code>`).join(' ') : '<code>none</code>'}</span>
        <span>workflow <code>${esc(c.workflow)}</code></span>
      </div>
      ${c.review_note ? `<p class="field-hint">Reviewer note: ${esc(c.review_note)}</p>` : ''}
      <div class="row-actions" style="margin-top:12px">
        ${inReview && canApproveAgents ? `
          <button class="button" onclick="reviewCustomAgent('${c.id}', true)">Approve</button>
          <button class="button ghost" onclick="reviewCustomAgent('${c.id}', false)">Reject</button>` : ''}
        ${canEdit ? `<button class="button ghost small"
          onclick="openCustomAgentDialog('${c.id}')">Edit</button>` : ''}
        <button class="button ghost small" onclick="exportCustomAgent('${c.id}')">Download</button>
        ${canApproveAgents ? `<button class="button ghost small"
          onclick="deleteCustomAgent('${c.id}')">Delete</button>` : ''}
      </div>
    </div>`;
}

function openCustomAgentDialog(id) {
  const c = id ? customAgents.find(x => x.id === id) || {} : {};
  const editing = Boolean(id);
  const selected = new Set(c.tools || []);
  showDialog(editing ? `Edit ${c.name}` : 'New custom agent', `
    <p class="dialog-note">All agents are Claude Agent SDK based. Tools are picked from the
      grantable set below — a shell and the network are never options — and the risk tier is
      derived from what you select. ${canApproveAgents ? 'As an admin, your agent is approved on save.'
        : 'Your agent is submitted for an admin to approve before it can run.'}</p>

    <label for="ca-name">Name</label>
    <input id="ca-name" class="draft-field" value="${esc(c.name || '')}" placeholder="e.g. Log summariser" />

    <label for="ca-purpose">Purpose (one line)</label>
    <input id="ca-purpose" class="draft-field" value="${esc(c.purpose || '')}"
      placeholder="What this agent decides, in a sentence." />

    <label for="ca-explanation">Explanation (optional)</label>
    <textarea id="ca-explanation" class="draft-field" rows="2"
      placeholder="When it runs and what it uses.">${esc(c.explanation || '')}</textarea>

    <label for="ca-workflow">Workflow</label>
    <select id="ca-workflow" class="draft-field">
      ${[['incident_remediation','Incident remediation'],['ticket_to_pr','Ticket → PR'],['both','Both']]
        .map(([v,l]) => `<option value="${v}" ${c.workflow === v ? 'selected' : ''}>${l}</option>`).join('')}
    </select>

    <label for="ca-model">Model</label>
    <select id="ca-model" class="draft-field">
      ${['claude-haiku-4-5','claude-sonnet-5','claude-opus-4-8']
        .map(m => `<option ${c.model === m ? 'selected' : ''}>${m}</option>`).join('')}
    </select>

    <label>Tools <span class="field-hint">tier is derived from these</span></label>
    <div class="tool-checklist">
      ${grantableTools.map(t => `<label class="tool-check">
        <input type="checkbox" value="${esc(t.name)}" ${selected.has(t.name) ? 'checked' : ''} />
        <code>${esc(t.name)}</code><span class="tool-tier">${esc(t.tier.replace('_',' '))}</span>
      </label>`).join('')}
    </div>

    <label for="ca-prompt">Instructions (system prompt)</label>
    <textarea id="ca-prompt" class="draft-editor" style="min-height:130px"
      placeholder="What the agent should do. The platform's safety rules are prepended automatically.">${esc(c.system_prompt || '')}</textarea>

    <p class="row-actions">
      <button class="button" onclick="saveCustomAgent(${editing ? `'${id}'` : 'null'})">
        ${editing ? 'Save' : (canApproveAgents ? 'Create' : 'Submit for review')}</button>
      <button class="button ghost" onclick="closeDialog()">Cancel</button></p>`);
}

async function saveCustomAgent(id) {
  const tools = [...document.querySelectorAll('.tool-checklist input:checked')].map(i => i.value);
  const body = {
    name: el('ca-name').value.trim(),
    purpose: el('ca-purpose').value.trim(),
    explanation: el('ca-explanation').value.trim(),
    workflow: el('ca-workflow').value,
    model: el('ca-model').value,
    system_prompt: el('ca-prompt').value.trim(),
    tools,
  };
  if (!body.name || body.purpose.length < 8 || body.system_prompt.length < 20) {
    return toast('Name, a one-line purpose, and real instructions are all required', false);
  }
  const response = await fetch(id ? `/api/agents/custom/${id}` : '/api/agents/custom', {
    method: id ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!response.ok) return toast((await response.json()).detail || 'could not save', false);
  closeDialog();
  toast(id ? 'Agent saved' : (canApproveAgents ? 'Agent created' : 'Submitted for review'));
  renderAgents();
}

async function reviewCustomAgent(id, approved) {
  const note = approved ? (window.prompt('Note (optional)') ?? '')
    : (window.prompt('Why are you rejecting it?') ?? '');
  const response = await fetch(`/api/agents/custom/${id}/review`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ approved, note: note || null }),
  });
  if (!response.ok) return toast((await response.json()).detail || 'could not record', false);
  toast(approved ? 'Agent approved' : 'Agent rejected');
  renderAgents();
}

async function deleteCustomAgent(id) {
  const c = customAgents.find(x => x.id === id);
  if (!window.confirm(`Delete the custom agent "${c ? c.name : id}"?`)) return;
  const response = await fetch(`/api/agents/custom/${id}`, { method: 'DELETE' });
  if (!response.ok) return toast((await response.json()).detail || 'could not delete', false);
  toast('Agent deleted');
  renderAgents();
}

function exportCustomAgent(id) { window.location = `/api/agents/custom/${id}/export`; }

async function selectAgentsTab(tab, restoreFocus = false) {
  agentsTab = tab;
  await renderAgents();
  if (restoreFocus) {
    const active = el(`agent-tab-${tab}`);
    if (active) active.focus();
  }
}

function handleAgentTabKey(event, tab) {
  const tabs = ['incident_remediation', 'ticket_to_pr'];
  const index = tabs.indexOf(tab);
  let next = null;
  if (event.key === 'ArrowRight') next = tabs[(index + 1) % tabs.length];
  if (event.key === 'ArrowLeft') next = tabs[(index - 1 + tabs.length) % tabs.length];
  if (event.key === 'Home') next = tabs[0];
  if (event.key === 'End') next = tabs[tabs.length - 1];
  if (!next) return;
  event.preventDefault();
  selectAgentsTab(next, true);
}

function exportAllAgents() {
  window.location = '/api/agents/export/bundle';
}

function exportAgent(id) {
  window.location = `/api/agents/${id}/export`;
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
      <div class="row-actions" style="margin-top:12px">
        <button class="button" onclick="openAgentDialog('${esc(agent.id)}')"
          ${canEdit ? '' : 'disabled title="Requires admin — switch role in the sidebar"'}>Edit</button>
        <button class="button ghost" onclick="toggleAgent('${esc(agent.id)}')"
          ${canEdit ? '' : 'disabled title="Requires admin — switch role in the sidebar"'}>
          ${agent.enabled ? 'Disable' : 'Enable'}</button>
        <button class="button ghost" onclick="showPrompt('${esc(agent.id)}')">View prompt</button>
        <button class="button ghost" onclick="exportAgent('${esc(agent.id)}')">Download</button>
        ${agent.customised && canEdit
          ? `<button class="button ghost" onclick="resetAgent('${esc(agent.id)}')">Reset</button>` : ''}
      </div>
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

let currentAgentId = null;

function openAgentDialog(id) {
  const agent = agentCache.find(a => a.id === id);
  currentAgentId = id;
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

    <label for="ag-prompt">Instructions (the agent's task prompt)</label>
    <textarea id="ag-prompt" class="draft-editor" style="min-height:150px"
      placeholder="What this agent should do.">${esc(agent.custom_prompt || agent.default_prompt)}</textarea>
    <p class="field-hint">
      Replaces the shipped instructions entirely — rewrite freely.
      ${agent.custom_prompt
        ? '<a href="#" onclick="restoreDefaultPrompt(event)">Restore the shipped instructions</a>'
        : 'Currently the shipped default.'}
    </p>

    <label for="ag-guidance">Additional guidance <span class="field-hint" style="display:inline">(optional)</span></label>
    <textarea id="ag-guidance" class="draft-editor" style="min-height:80px"
      placeholder="Extra notes appended below the instructions — domain conventions, what to prefer when evidence is ambiguous.">${esc(agent.extra_guidance || '')}</textarea>
    <p class="field-hint">Appended in a lower-authority block, below the safety rules.</p>

    <p class="locked-note">
      <strong>Not editable by design:</strong> tools
      (<code>${agent.tools.join('</code> <code>') || 'none'}</code>), access tier
      (<code>${esc(agent.tier)}</code>) and the safety preamble that sits above your
      instructions. Text that tries to countermand those rules is rejected — otherwise
      editing an agent would be a way around them.
      <a href="#" onclick="showPrompt('${esc(id)}');closeDialog();return false">See the composed prompt</a>.
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
        // Only store a prompt when it actually differs from the shipped one,
        // so "customised" means something.
        custom_prompt: promptDiffersFromDefault(agent) ? el('ag-prompt').value.trim() : null,
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

function promptDiffersFromDefault(agent) {
  const current = el('ag-prompt').value.trim();
  return current !== '' && current !== agent.default_prompt.trim();
}

function restoreDefaultPrompt(event) {
  event.preventDefault();
  el('ag-prompt').value = agentCache.find(a => a.id === currentAgentId).default_prompt;
}

async function resetAgent(id) {
  if (!confirm(`Reset ${id} to its shipped defaults?`)) return;
  await fetch(`/api/agents/${id}/reset`, { method: 'POST' });
  renderAgents();
}

// --- connections ------------------------------------------------------------

let connectionCache = [];

// Compact vector brand marks are inline so they inherit the app's crisp
// rendering at every scale and never depend on an external image request.
const CONNECTOR_LOGOS = {
  servicenow: `<svg viewBox="0 0 32 32" class="connector-logo" aria-hidden="true">
    <rect x="2" y="2" width="28" height="28" rx="10" fill="#62D84E"/>
    <path d="M7.5 20.5v-3.1a8.5 8.5 0 0 1 17 0v3.1c0 1.3-1.5 2-2.5 1.2l-2.4-1.9a5.8 5.8 0 0 1-7.2 0L10 21.7c-1 .8-2.5.1-2.5-1.2z" fill="#fff"/>
    <circle cx="16" cy="16.5" r="2.4" fill="#23834A"/></svg>`,
  jira: `<svg viewBox="0 0 32 32" class="connector-logo" aria-hidden="true">
    <path d="M23.5 5H15a4.9 4.9 0 0 0 4.9 4.9h1.6v1.5A4.9 4.9 0 0 0 26.4 16V7.9A2.9 2.9 0 0 0 23.5 5z" fill="#2684FF"/>
    <path d="M19.3 9.3h-8.5a4.9 4.9 0 0 0 4.9 4.9h1.6v1.5a4.9 4.9 0 0 0 4.9 4.9v-8.4a2.9 2.9 0 0 0-2.9-2.9z" fill="#2684FF" opacity=".8"/>
    <path d="M15.1 13.6H6.6a4.9 4.9 0 0 0 4.9 4.9h1.6V20a4.9 4.9 0 0 0 4.9 4.9v-8.4a2.9 2.9 0 0 0-2.9-2.9z" fill="#2684FF" opacity=".6"/></svg>`,
  splunk: `<svg viewBox="0 0 32 32" class="connector-logo" aria-hidden="true">
    <rect x="2" y="2" width="28" height="28" rx="8" fill="#111820" stroke="#64748B"/>
    <path d="m10 9 9 7-9 7" fill="none" stroke="#65A637" stroke-width="3.2"
      stroke-linecap="round" stroke-linejoin="round"/>
    <path d="M20.5 23h3" stroke="#B8E986" stroke-width="2.5" stroke-linecap="round"/></svg>`,
  datadog: `<svg viewBox="0 0 32 32" class="connector-logo" aria-hidden="true">
    <rect x="2" y="2" width="28" height="28" rx="8" fill="#632CA6"/>
    <path d="m8 12 3.4-4 3.2 2.2h3L20.8 8l3.4 4-1.4 2.5v5.2c0 2.6-2.2 4.8-4.8 4.8h-4c-2.6 0-4.8-2.2-4.8-4.8v-5.2L8 12z" fill="#fff"/>
    <circle cx="13.2" cy="16.2" r="1.2" fill="#632CA6"/>
    <circle cx="18.8" cy="16.2" r="1.2" fill="#632CA6"/>
    <path d="M14 20h4" stroke="#632CA6" stroke-width="1.8" stroke-linecap="round"/></svg>`,
  dynatrace: `<svg viewBox="0 0 32 32" class="connector-logo" aria-hidden="true">
    <path d="M4 8.5 15.5 3 22 10.5 13.5 16z" fill="#1496FF"/>
    <path d="m15.5 3 12.5 5.5-6 11-6.5-9z" fill="#73BE28"/>
    <path d="m4 8.5 9.5 7.5L10 29 3 21z" fill="#6F2DA8"/>
    <path d="m13.5 16 8.5 3.5L10 29z" fill="#00A1B2"/>
    <path d="m22 10.5 6-2v12.8L10 29l12-9.5z" fill="#B4DC00" opacity=".9"/></svg>`,
};

const CONNECTOR_LABELS = {
  servicenow: 'ServiceNow',
  jira: 'Jira',
  splunk: 'Splunk',
  datadog: 'Datadog',
  dynatrace: 'Dynatrace',
};

async function renderConnections(token = viewToken) {
  el('view').innerHTML = '<div class="empty">Loading…</div>';
  let data;
  try {
    data = await (await fetch('/api/connections')).json();
  } catch {
    el('view').innerHTML = '<div class="empty">Connections could not be loaded.</div>';
    return;
  }
  if (!viewIsCurrent(token)) return;
  connectionCache = data.connections;
  const isAdmin = principal.role === 'admin';
  const canTest = principal.role !== 'viewer';
  el('view').innerHTML = `
    <div class="conn-bar">
      <span class="conn-bar-title">Connections</span>
      <div class="menu-wrap">
        <button class="button small" id="add-menu-btn" onclick="toggleAddMenu(event)"
          ${isAdmin ? '' : 'disabled'} aria-haspopup="true" aria-controls="add-menu"
          aria-expanded="false">Add connection <i class="caret" aria-hidden="true">▾</i></button>
        <div class="menu hidden" id="add-menu" role="menu">
          <button class="menu-item" role="menuitem" onclick="openConnectionDialog(null,'servicenow')">
            ${CONNECTOR_LOGOS.servicenow}<span>ServiceNow</span></button>
          <button class="menu-item" role="menuitem" onclick="openConnectionDialog(null,'jira')">
            ${CONNECTOR_LOGOS.jira}<span>Jira</span></button>
          <button class="menu-item" role="menuitem" onclick="openConnectionDialog(null,'splunk')">
            ${CONNECTOR_LOGOS.splunk}<span>Splunk</span></button>
          <button class="menu-item" role="menuitem" onclick="openConnectionDialog(null,'datadog')">
            ${CONNECTOR_LOGOS.datadog}<span>Datadog</span></button>
          <button class="menu-item" role="menuitem" onclick="openConnectionDialog(null,'dynatrace')">
            ${CONNECTOR_LOGOS.dynatrace}<span>Dynatrace</span></button>
        </div>
      </div>
    </div>
    ${data.connections.length
      ? `<div class="conn-list">${data.connections.map(c => connectionRow(c, isAdmin, canTest)).join('')}</div>`
      : `<div class="conn-empty">No connections yet. Use <strong>Add connection</strong> to point a
         workflow at ServiceNow, Jira, or an observability platform.</div>`}
    ${data.environment_usable ? `<p class="field-hint">A ServiceNow connection is also configured
      from environment variables (<code>${esc(data.environment_auth_method)}</code> auth); a
      workflow with none selected falls back to it.</p>` : ''}`;
}

function toggleAddMenu(event) {
  event.stopPropagation();
  const menu = el('add-menu');
  const btn = event.currentTarget;
  const opening = menu.classList.contains('hidden');
  menu.classList.toggle('hidden', !opening);
  if (btn && btn.setAttribute) btn.setAttribute('aria-expanded', String(opening));
  if (opening) {
    // Move focus to the first choice so the keyboard can drive it, and wire an
    // outside-click and Escape close.
    const first = menu.querySelector('.menu-item');
    if (first) first.focus();
    setTimeout(() => document.addEventListener('click', closeAddMenu, { once: true }), 0);
    document.addEventListener('keydown', addMenuEscape);
  }
}
function addMenuEscape(event) {
  if (event.key === 'Escape') closeAddMenu();
}
function closeAddMenu() {
  const m = el('add-menu');
  if (m) m.classList.add('hidden');
  const btn = el('add-menu-btn');
  if (btn) btn.setAttribute('aria-expanded', 'false');
  document.removeEventListener('keydown', addMenuEscape);
}

function connectionRow(c, isAdmin, canTest) {
  const queue = c.kind === 'jira' ? (c.project_key || c.jql)
    : c.kind === 'splunk' ? c.search_query
    : c.kind === 'datadog' ? c.service_filter
    : c.kind === 'dynatrace' ? c.entity_selector
    : c.assignment_group;
  const status = c.last_test_ok === true ? 'on' : c.last_test_ok === false ? 'off' : 'unknown';
  const statusText = c.last_test_ok === true ? 'Connected'
    : c.last_test_ok === false ? 'Failing' : 'Untested';
  return `
    <div class="conn-row clickable" role="button" tabindex="0" data-activate
      aria-label="${esc(c.name)} — ${statusText}. Edit connection."
      onclick="openConnectionDialog('${c.id}')">
      <span class="conn-logo" title="${esc(CONNECTOR_LABELS[c.kind] || c.kind)}">${
        CONNECTOR_LOGOS[c.kind] || ''}</span>
      <span class="conn-status ${status}"><span class="conn-status-dot"></span>${statusText}</span>
      <div class="conn-main">
        <div class="conn-name">${esc(c.name)}
          ${queue ? `<span class="conn-queue">${esc(queue)}</span>` : ''}</div>
        <div class="conn-sub"><code>${esc(c.base_url)}</code>${
          c.username ? ` · ${esc(c.username)}` : ''}</div>
      </div>
      <div class="conn-actions" onclick="event.stopPropagation()">
        <button class="icon-button" title="Edit" onclick="openConnectionDialog('${c.id}')"
          ${isAdmin ? '' : 'disabled'}><i class="ti">✎</i></button>
        <button class="icon-button danger" title="Delete" onclick="deleteConnection('${c.id}')"
          ${isAdmin ? '' : 'disabled'}>×</button>
      </div>
    </div>`;
}

function openConnectionDialog(id, kind) {
  const c = connectionCache.find(x => x.id === id) || {};
  const editing = Boolean(id);
  kind = c.kind || kind || 'servicenow';
  if (kind === 'jira') return openJiraDialog(c, id, editing);
  if (['splunk', 'datadog', 'dynatrace'].includes(kind)) {
    return openObservabilityDialog(c, id, editing, kind);
  }
  return openServiceNowDialog(c, id, editing);
}

function openServiceNowDialog(c, id, editing) {
  showDialog(editing ? `Edit ${c.name}` : 'Add ServiceNow connection', `
    <input type="hidden" id="cn-kind" value="servicenow" />
    <label for="cn-name">Connection name</label>
    <input id="cn-name" class="draft-field" value="${esc(c.name || '')}"
      placeholder="e.g. Production, or Dev 385636" />
    <p class="field-hint">Whatever you will recognise when picking it on a workflow.</p>

    <label for="cn-url">Instance URL</label>
    <input id="cn-url" class="draft-field" value="${esc(c.base_url || '')}"
      placeholder="https://dev385636.service-now.com" />

    <label for="cn-auth">Authentication</label>
    <select id="cn-auth" class="draft-field" onchange="toggleOauthFields()">
      <option value="basic" ${c.auth_type !== 'oauth' ? 'selected' : ''}>Basic — username and password</option>
      <option value="oauth" ${c.auth_type === 'oauth' ? 'selected' : ''}>OAuth — client credentials</option>
    </select>

    <label for="cn-user">Username</label>
    <input id="cn-user" class="draft-field" value="${esc(c.username || '')}" autocomplete="off" />

    <label for="cn-pass">Password</label>
    <input id="cn-pass" class="draft-field" type="password" autocomplete="new-password"
      placeholder="${c.secrets_set && c.secrets_set.password ? 'unchanged — leave blank to keep it' : ''}" />
    <p class="field-hint">Set the ServiceNow user's <strong>identity type to Machine</strong>.
      Human accounts are refused for REST basic auth, and the error is identical to a wrong
      password.</p>

    <div id="cn-oauth" class="${c.auth_type === 'oauth' ? '' : 'hidden'}">
      <label for="cn-cid">Client ID</label>
      <input id="cn-cid" class="draft-field" value="${esc(c.client_id || '')}" autocomplete="off" />
      <label for="cn-csec">Client secret</label>
      <input id="cn-csec" class="draft-field" type="password" autocomplete="new-password"
        placeholder="${c.secrets_set && c.secrets_set.client_secret ? 'unchanged' : ''}" />
      <p class="field-hint">From System OAuth → Application Registry → “Create an OAuth API
        endpoint for external clients”.</p>
    </div>

    <label for="cn-queue">Monitored queue (assignment group)</label>
    <input id="cn-queue" class="draft-field" value="${esc(c.assignment_group || '')}"
      placeholder="e.g. IPM_MQ_S_ADMIN" />
    <p class="field-hint">New active incidents assigned to this group trigger the workflow.
      Leave empty to start runs by hand instead.</p>

    <label for="cn-extra">Extra filter (optional)</label>
    <input id="cn-extra" class="draft-field" value="${esc(c.extra_query || '')}"
      placeholder="priority&lt;=2" />
    <p class="field-hint">A ServiceNow encoded query, combined with the queue above.</p>

    <div class="dialog-test">
      <button class="button small" onclick="testDraft(${editing ? `'${id}'` : 'null'})">Test connection</button>
      <span class="dialog-test-result" id="draft-test-result"></span>
    </div>
    <p class="row-actions">
      <button class="button" onclick="saveConnection(${editing ? `'${id}'` : 'null'})">
        ${editing ? 'Save changes' : 'Create connection'}</button>
      <button class="button ghost" onclick="closeDialog()">Cancel</button></p>`);
}

function openJiraDialog(c, id, editing) {
  showDialog(editing ? `Edit ${c.name}` : 'Add Jira connection', `
    <input type="hidden" id="cn-kind" value="jira" />
    <label for="cn-name">Connection name</label>
    <input id="cn-name" class="draft-field" value="${esc(c.name || '')}"
      placeholder="e.g. Engineering Jira" />

    <label for="cn-url">Jira site URL</label>
    <input id="cn-url" class="draft-field" value="${esc(c.base_url || '')}"
      placeholder="https://your-company.atlassian.net" />

    <label for="cn-user">Account email</label>
    <input id="cn-user" class="draft-field" type="email" value="${esc(c.username || '')}"
      autocomplete="off" placeholder="you@company.com" />

    <label for="cn-token">API token</label>
    <input id="cn-token" class="draft-field" type="password" autocomplete="new-password"
      placeholder="${c.secrets_set && c.secrets_set.api_token ? 'unchanged — leave blank to keep it' : ''}" />
    <p class="field-hint">Jira Cloud authenticates the REST API with your <strong>email and an
      API token</strong> — not your password. Create one at id.atlassian.com → Security →
      API tokens.</p>

    <label for="cn-project">Monitored project key</label>
    <input id="cn-project" class="draft-field" value="${esc(c.project_key || '')}"
      placeholder="e.g. OPS" />
    <p class="field-hint">New issues in this project trigger the workflow. Leave empty to
      start runs by hand.</p>

    <label for="cn-jql">Advanced: full JQL (optional)</label>
    <input id="cn-jql" class="draft-field" value="${esc(c.jql || '')}"
      placeholder='project = OPS AND labels = automate' />
    <p class="field-hint">Overrides the project key when set. Any valid JQL.</p>

    <div class="dialog-test">
      <button class="button small" onclick="testDraft(${editing ? `'${id}'` : 'null'})">Test connection</button>
      <span class="dialog-test-result" id="draft-test-result"></span>
    </div>
    <p class="row-actions">
      <button class="button" onclick="saveConnection(${editing ? `'${id}'` : 'null'})">
        ${editing ? 'Save changes' : 'Create connection'}</button>
      <button class="button ghost" onclick="closeDialog()">Cancel</button></p>`);
}

function openObservabilityDialog(c, id, editing, kind) {
  const definitions = {
    splunk: {
      label: 'Splunk',
      urlLabel: 'Splunk management URL',
      urlPlaceholder: 'https://splunk.example.com:8089',
      secretLabel: 'Access token',
      secretHint: 'Create a Splunk authentication token with access to the indexes this workflow monitors.',
      scopeLabel: 'Default search (optional)',
      scopePlaceholder: 'index=prod service=checkout',
      scopeField: 'search_query',
    },
    datadog: {
      label: 'Datadog',
      urlLabel: 'Datadog API URL',
      urlPlaceholder: 'https://api.datadoghq.com',
      secretLabel: 'API key',
      secretHint: 'Use an API key and application key with the minimum scopes needed by this workspace.',
      scopeLabel: 'Service or tag filter (optional)',
      scopePlaceholder: 'service:checkout env:prod',
      scopeField: 'service_filter',
    },
    dynatrace: {
      label: 'Dynatrace',
      urlLabel: 'Dynatrace environment URL',
      urlPlaceholder: 'https://abc123.live.dynatrace.com',
      secretLabel: 'API token',
      secretHint: 'The token needs the problems.read scope for the connection check and incident context.',
      scopeLabel: 'Entity selector (optional)',
      scopePlaceholder: 'type(SERVICE),tag(prod)',
      scopeField: 'entity_selector',
    },
  };
  const d = definitions[kind];
  const hasPrimarySecret = c.secrets_set && (
    kind === 'datadog' ? c.secrets_set.api_key : c.secrets_set.access_token);
  showDialog(editing ? `Edit ${c.name}` : `Add ${d.label} connection`, `
    <input type="hidden" id="cn-kind" value="${kind}" />
    <label for="cn-name">Connection name</label>
    <input id="cn-name" class="draft-field" value="${esc(c.name || '')}"
      placeholder="e.g. Production ${d.label}" />

    <label for="cn-url">${d.urlLabel}</label>
    <input id="cn-url" class="draft-field" value="${esc(c.base_url || '')}"
      placeholder="${d.urlPlaceholder}" />

    <label for="cn-token">${d.secretLabel}</label>
    <input id="cn-token" class="draft-field" type="password" autocomplete="new-password"
      placeholder="${hasPrimarySecret ? 'unchanged — leave blank to keep it' : ''}" />
    <p class="field-hint">${d.secretHint}</p>

    ${kind === 'datadog' ? `
      <label for="cn-app-key">Application key</label>
      <input id="cn-app-key" class="draft-field" type="password" autocomplete="new-password"
        placeholder="${c.secrets_set && c.secrets_set.app_key
          ? 'unchanged — leave blank to keep it' : ''}" />` : ''}

    <label for="cn-scope">${d.scopeLabel}</label>
    <input id="cn-scope" class="draft-field" value="${esc(c[d.scopeField] || '')}"
      placeholder="${d.scopePlaceholder}" />
    <p class="field-hint">Used as the default scope when this connection supplies operational context.</p>

    <div class="dialog-test">
      <button class="button small" onclick="testDraft(${editing ? `'${id}'` : 'null'})">Test connection</button>
      <span class="dialog-test-result" id="draft-test-result"></span>
    </div>
    <p class="row-actions">
      <button class="button" onclick="saveConnection(${editing ? `'${id}'` : 'null'})">
        ${editing ? 'Save changes' : 'Create connection'}</button>
      <button class="button ghost" onclick="closeDialog()">Cancel</button></p>`);
}

function toggleOauthFields() {
  el('cn-oauth').classList.toggle('hidden', el('cn-auth').value !== 'oauth');
}

function _connectionForm(id) {
  const kind = el('cn-kind').value;
  if (kind === 'jira') {
    return { kind, name: el('cn-name').value.trim(), base_url: el('cn-url').value.trim(),
      username: el('cn-user').value.trim(), api_token: el('cn-token').value || null,
      project_key: el('cn-project').value.trim(), jql: el('cn-jql').value.trim() };
  }
  if (['splunk', 'datadog', 'dynatrace'].includes(kind)) {
    const shared = { kind, name: el('cn-name').value.trim(),
      base_url: el('cn-url').value.trim() };
    if (kind === 'splunk') {
      return { ...shared, access_token: el('cn-token').value || null,
        search_query: el('cn-scope').value.trim() };
    }
    if (kind === 'datadog') {
      return { ...shared, api_key: el('cn-token').value || null,
        app_key: el('cn-app-key').value || null,
        service_filter: el('cn-scope').value.trim() };
    }
    return { ...shared, access_token: el('cn-token').value || null,
      entity_selector: el('cn-scope').value.trim() };
  }
  return { kind: 'servicenow', name: el('cn-name').value.trim(),
    base_url: el('cn-url').value.trim(), auth_type: el('cn-auth').value,
    username: el('cn-user').value.trim(), password: el('cn-pass').value || null,
    client_id: el('cn-cid') ? el('cn-cid').value.trim() : '',
    client_secret: el('cn-csec') ? (el('cn-csec').value || null) : null,
    assignment_group: el('cn-queue').value.trim(), extra_query: el('cn-extra').value.trim() };
}

async function testDraft(id) {
  const out = el('draft-test-result');
  const body = _connectionForm(id);
  if (!body.base_url) { out.innerHTML = '<span class="tier-badge bad">failed</span> Enter the URL first.'; return; }
  out.innerHTML = '<span class="muted">Testing…</span>';
  const url = '/api/connections/test-draft' + (id ? `?connection_id=${id}` : '');
  try {
    const r = await (await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body) })).json();
    out.innerHTML = r.ok
      ? `<span class="tier-badge ok">connected</span> ${esc(r.detail)}`
      : `<span class="tier-badge bad">failed</span> ${esc(r.detail)}`;
  } catch {
    out.innerHTML = '<span class="tier-badge bad">failed</span> the test could not run';
  }
}

async function saveConnection(id) {
  const body = _connectionForm(id);
  if (!body.name || !body.base_url) return toast('Name and URL are required', false);
  const response = await fetch(id ? `/api/connections/${id}` : '/api/connections', {
    method: id ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!response.ok) return toast((await response.json()).detail || 'could not save', false);
  closeDialog();
  toast(id ? 'Connection updated' : 'Connection created');
  renderConnections();
}

async function deleteConnection(id) {
  const c = connectionCache.find(x => x.id === id);
  if (!window.confirm(`Delete the connection "${c ? c.name : id}"?`)) return;
  const response = await fetch(`/api/connections/${id}`, { method: 'DELETE' });
  if (!response.ok) return toast((await response.json()).detail || 'could not delete', false);
  toast('Connection deleted');
  renderConnections();
}

async function testStoredConnection(id) {
  const target = el(`conn-result-${id}`);
  if (target) target.textContent = 'Testing…';
  const response = await fetch(`/api/connections/${id}/test`, { method: 'POST' });
  const result = await response.json();
  if (target) {
    target.innerHTML = result.ok
      ? `<span class="tier-badge ok">connected</span> ${esc(result.detail)}`
      : `<span class="tier-badge bad">failed</span> ${esc(result.detail)}`;
  }
  renderConnections();
}

// --- users -------------------------------------------------------------------

async function renderUsers(token = viewToken) {
  el('view').innerHTML = '<div class="empty">Loading…</div>';
  if (principal.role !== 'admin') {
    el('view').innerHTML = `<div class="pipeline-note">Managing users requires the admin
      role. You are signed in as <strong>${esc(principal.role)}</strong>.</div>`;
    return;
  }
  let data;
  let registrations;
  try {
    const [usersResponse, registrationsResponse] = await Promise.all([
      fetch('/api/users'),
      fetch('/api/registration-requests'),
    ]);
    if (!usersResponse.ok || !registrationsResponse.ok) {
      throw new Error('could not load user administration');
    }
    [data, registrations] = await Promise.all([
      usersResponse.json(),
      registrationsResponse.json(),
    ]);
  } catch (error) {
    if (viewIsCurrent(token)) {
      el('view').innerHTML = `<div class="pipeline-note">${esc(error.message)}</div>`;
    }
    return;
  }
  if (!viewIsCurrent(token)) return;
  const active = data.users.filter(u => u.active).length;
  const pending = registrations.requests.filter(r => r.status === 'pending').length;
  el('view').innerHTML = `
    <div class="pipeline-note">
      Roles are enforced by the server on every request, not by hiding buttons.
      <strong>Viewer</strong> reads · <strong>Operator</strong> starts runs ·
      <strong>Approver</strong> decides on human gates · <strong>Admin</strong> manages
      everything.
    </div>
    <section class="user-admin-section" aria-labelledby="access-requests-title">
      <div class="table-head">
        <div>
          <h2 id="access-requests-title">Access requests</h2>
          <span class="table-count">${pending} awaiting review</span>
        </div>
      </div>
      ${registrations.requests.length ? `
        <div class="table-scroll">
          <table class="data-table">
            <thead>
              <tr>
                <th>Status</th><th>Name</th><th>Email</th><th>Requested</th><th></th>
              </tr>
            </thead>
            <tbody>${registrations.requests.map(registrationRow).join('')}</tbody>
          </table>
        </div>` : `
        <div class="empty request-empty">No access requests yet.</div>`}
      <p class="field-hint notification-availability">
        Applicant notification can be requested during approval or rejection.
        Email delivery is not configured yet, so the decision is recorded but no email is sent.
      </p>
    </section>
    <section class="user-admin-section" aria-labelledby="accounts-title">
    <div class="table-head">
      <div>
        <h2 id="accounts-title">Accounts</h2>
        <span class="table-count">${active} active · ${data.users.length - active} inactive</span>
      </div>
      <button class="button" onclick="openUserDialog()">Invite user</button>
    </div>
    <div class="table-scroll">
      <table class="data-table">
        <thead>
          <tr>
            <th class="col-status">Status</th>
            <th>Name</th><th>Email</th><th>Role</th><th>Last sign-in</th><th></th>
          </tr>
        </thead>
        <tbody>
          ${data.users.map(userRow).join('')}
        </tbody>
      </table>
    </div>
    </section>`;
  window.__users = data.users;
  window.__registrationRequests = registrations.requests;
  window.__registrationRoles = registrations.roles;
}

function registrationRow(request) {
  const requested = new Date(request.requested_at * 1000).toLocaleString();
  const status = `<span class="request-status ${esc(request.status)}">
    ${esc(request.status)}</span>`;
  const notification = request.notify_requested
    ? `<span class="row-flag ${request.notification_status === 'not_configured' ? 'bad' : ''}">
        notify: ${esc(request.notification_status.replace(/_/g, ' '))}</span>`
    : '';
  const actions = request.status === 'pending'
    ? `<button class="button small" onclick="openRegistrationReview('${request.id}', 'approve')">
         Approve</button>
       <button class="button ghost small" onclick="openRegistrationReview('${request.id}', 'reject')">
         Reject</button>`
    : `<span class="muted">${request.reviewed_at
        ? new Date(request.reviewed_at * 1000).toLocaleString() : ''}</span>`;
  return `
    <tr>
      <td>${status} ${notification}</td>
      <td>${esc(request.display_name)}</td>
      <td><code>${esc(request.email)}</code></td>
      <td class="muted">${requested}</td>
      <td class="col-action request-actions">${actions}</td>
    </tr>`;
}

function openRegistrationReview(id, action) {
  const request = (window.__registrationRequests || []).find(item => item.id === id);
  if (!request || request.status !== 'pending') return;
  const approving = action === 'approve';
  showDialog(`${approving ? 'Approve' : 'Reject'} access request`, `
    <div class="request-review-identity">
      <strong>${esc(request.display_name)}</strong>
      <code>${esc(request.email)}</code>
    </div>
    ${approving ? `
      <label for="registration-role">Role</label>
      <select id="registration-role" class="draft-field">
        ${(window.__registrationRoles || ROLES).map(role =>
          `<option value="${esc(role)}">${esc(role)}</option>`).join('')}
      </select>
      <p class="field-hint">The applicant requested access, not a role. You assign the
        least privilege they need.</p>` : `
      <p class="dialog-note">Rejecting keeps a decision record and creates no account.</p>`}
    <label for="registration-note">Decision note (optional)</label>
    <textarea id="registration-note" class="draft-field" rows="3"
      maxlength="500" placeholder="Reason or onboarding context"></textarea>
    <div class="field-row">
      <label class="switch">
        <input type="checkbox" id="registration-notify" />
        <span>Notify applicant</span>
      </label>
    </div>
    <p class="field-hint notification-warning">
      Email delivery is not configured. This records the notification request,
      but no message will be sent.
    </p>
    <p class="row-actions">
      <button class="button ${approving ? '' : 'danger'}"
        onclick="submitRegistrationReview('${id}', '${action}')">
        ${approving ? 'Approve and create account' : 'Reject request'}
      </button>
      <button class="button ghost" onclick="closeDialog()">Cancel</button>
    </p>`);
}

async function submitRegistrationReview(id, action) {
  const notify = el('registration-notify').checked;
  const body = {
    note: el('registration-note').value.trim() || null,
    notify_applicant: notify,
  };
  if (action === 'approve') body.role = el('registration-role').value;
  const response = await fetch(`/api/registration-requests/${id}/${action}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const result = await response.json();
  if (!response.ok) return toast(result.detail || 'could not review request', false);
  closeDialog();
  const notificationStatus = result.request.notification_status;
  const decision = action === 'approve' ? 'Account approved and created.' : 'Request rejected.';
  toast(notify && notificationStatus === 'not_configured'
    ? `${decision} Email was not sent because delivery is not configured.`
    : decision);
  renderUsers();
}

function userRow(u) {
  const status = u.active
    ? `<span class="status-dot on" title="Active"></span>Active`
    : `<span class="status-dot off" title="Deactivated"></span>Inactive`;
  const flags = [
    u.locked ? '<span class="row-flag bad">locked</span>' : '',
    u.must_change_password ? '<span class="row-flag">must change password</span>' : '',
  ].join('');
  const lastLogin = u.last_login_at
    ? new Date(u.last_login_at * 1000).toLocaleString()
    : '<span class="muted">never</span>';
  return `
    <tr class="${u.active ? '' : 'row-inactive'}">
      <td class="col-status">${status}</td>
      <td>${esc(u.display_name)} ${flags}</td>
      <td><code>${esc(u.email)}</code></td>
      <td><span class="role-pill ${u.role === 'admin' ? 'admin' : ''}">${esc(u.role)}</span></td>
      <td class="muted">${lastLogin}</td>
      <td class="col-action"><button class="button ghost small"
        onclick="openUserDialog('${u.id}')">Edit</button></td>
    </tr>`;
}

function openUserDialog(id) {
  const u = (window.__users || []).find(x => x.id === id) || {};
  const editing = Boolean(id);
  showDialog(editing ? `Edit ${u.display_name}` : 'Invite a user', `
    <label for="us-email">Email</label>
    <input id="us-email" class="draft-field" type="email" value="${esc(u.email || '')}"
      ${editing ? 'disabled' : ''} />
    <label for="us-name">Name</label>
    <input id="us-name" class="draft-field" value="${esc(u.display_name || '')}" />
    <label for="us-role">Role</label>
    <select id="us-role" class="draft-field">
      ${['viewer', 'operator', 'approver', 'admin'].map(r =>
        `<option value="${r}" ${u.role === r ? 'selected' : ''}>${r}</option>`).join('')}
    </select>
    <label for="us-pass">${editing ? 'Set a new password (optional)' : 'Initial password'}</label>
    <input id="us-pass" class="draft-field" type="password" autocomplete="new-password" />
    <p class="field-hint">At least 10 characters. You will know this password, so the user is
      asked to change it at first sign-in.</p>
    ${editing ? `<div class="field-row"><label class="switch">
      <input type="checkbox" id="us-active" ${u.active ? 'checked' : ''} />
      <span>Active</span></label>
      <span class="field-hint">Deactivating revokes access on the next request, not the next
        login. Users are never deleted — that would orphan the audit entries naming them.</span>
      </div>` : ''}
    <p class="row-actions">
      <button class="button" onclick="saveUser(${editing ? `'${id}'` : 'null'})">
        ${editing ? 'Save' : 'Create user'}</button>
      <button class="button ghost" onclick="closeDialog()">Cancel</button></p>`);
}

const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

async function saveUser(id) {
  const name = el('us-name').value.trim();
  if (!name) return toast('Enter a name', false);
  if (!id) {
    // Match the server: a valid email and a non-blank identity, caught here so
    // the invite does not round-trip only to be rejected.
    const email = el('us-email').value.trim();
    if (!EMAIL_RE.test(email)) return toast('Enter a valid email address', false);
    if (!el('us-pass').value) return toast('Set an initial password', false);
  }
  const body = id
    ? { display_name: name, role: el('us-role').value,
        active: el('us-active') ? el('us-active').checked : undefined,
        password: el('us-pass').value || undefined }
    : { email: el('us-email').value.trim(), display_name: name,
        role: el('us-role').value, password: el('us-pass').value };
  Object.keys(body).forEach(k => body[k] === undefined && delete body[k]);
  const response = await fetch(id ? `/api/users/${id}` : '/api/users', {
    method: id ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!response.ok) return toast((await response.json()).detail || 'could not save', false);
  closeDialog();
  toast(id ? 'User updated' : 'User created');
  renderUsers();
}

// --- feedback ---------------------------------------------------------------

let toastTimer;

// ok=true is a success/neutral toast; ok=false is an error. Callers already
// pass the flag — it was simply being ignored, so a failure looked identical to
// a success. An error also announces itself assertively to a screen reader.
function toast(message, ok = true) {
  const node = el('toast');
  node.textContent = message;
  node.classList.remove('ok', 'err');
  node.classList.add('on', ok ? 'ok' : 'err');
  node.setAttribute('aria-live', ok ? 'polite' : 'assertive');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.remove('on'), ok ? 3200 : 5000);
}

// --- lightweight dialog -----------------------------------------------------

function showDialog(title, bodyHtml) {
  closeDialog();
  document.body.insertAdjacentHTML('beforeend', `
    <dialog id="app-dialog" aria-labelledby="app-dialog-title">
      <button class="dialog-close" onclick="closeDialog()" aria-label="Close dialog">Close</button>
      <h3 id="app-dialog-title">${esc(title)}</h3>
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

async function renderAudit(token = viewToken) {
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
  // An invited user is holding a password the admin chose; the server blocks
  // every role-gated route until it is replaced, so force the change here
  // rather than letting them hit 403s.
  if (principal && principal.must_change_password) openPasswordDialog(true);
}

// --- live socket ------------------------------------------------------------

// Which views care about which engine events. Refreshing only the view being
// looked at keeps a busy run from re-rendering screens nobody is watching.
const EVENT_VIEWS = {
  run_started: ['runs'],
  run_step_started: ['runs'],
  run_step_finished: ['runs'],
  run_finished: ['runs', 'approvals'],
  run_resumed: ['runs'],
  approval_requested: ['approvals', 'runs'],
  approval_decided: ['approvals', 'runs'],
};

let refreshTimer = null;

function onEngineEvent(event) {
  const views = EVENT_VIEWS[event.type];
  if (!views || !views.includes(currentView)) return;
  // A run emits two events per node; coalesce so a fast run does not queue a
  // dozen renders behind itself.
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => switchView(currentView), 250);
}

function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/ws/events`);
  ws.onopen = () => { el('conn').textContent = 'live'; el('conn').className = 'conn live'; };
  ws.onmessage = (message) => {
    try {
      onEngineEvent(JSON.parse(message.data));
    } catch { /* a frame we do not understand is not worth breaking the socket */ }
  };
  ws.onclose = () => {
    el('conn').textContent = 'reconnecting…';
    el('conn').className = 'conn down';
    setTimeout(connect, 2000);
  };
  ws.onerror = () => ws.close();
}

// --- boot -------------------------------------------------------------------

// A container styled as clickable gets keyboard parity: any element with
// role="button" activates on Enter or Space, so a run row, a connection row and
// the approval callout behave the way a real button does without each having to
// wire its own handler.
document.addEventListener('keydown', (event) => {
  if (event.key !== 'Enter' && event.key !== ' ') return;
  const target = event.target.closest('[role="button"]');
  if (!target || target.tagName === 'BUTTON' || target.tagName === 'A') return;
  event.preventDefault();
  target.click();
});

// Escape closes the mobile navigation drawer as well as dialogs.
document.addEventListener('keydown', (event) => {
  // Enter or Space activates a role="button" container (run rows, home tiles,
  // connection rows) exactly as a real button would.
  if ((event.key === 'Enter' || event.key === ' ') &&
      event.target instanceof HTMLElement && event.target.matches('[data-activate]')) {
    event.preventDefault();
    event.target.click();
    return;
  }
  if (event.key === 'Escape') {
    const menu = el('add-menu');
    if (menu && !menu.classList.contains('hidden')) { closeAddMenu(); return; }
    if (el('app') && el('app').classList.contains('nav-open')) {
      closeNav();
      const toggle = el('nav-toggle');
      if (toggle) toggle.focus();
    }
  }
});

(async function boot() {
  syncThemeControls();
  try {
    const response = await fetch('/api/auth/me');
    if (response.ok) { principal = await response.json(); return showApp(); }
  } catch { /* fall through to the login gate */ }
  await loadAuthState();
  el('login-screen').classList.remove('hidden');
  const resetToken = resetTokenFromLocation();
  if (resetToken) showResetPasswordForm(resetToken);
})();
