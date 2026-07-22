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
  // Nothing here creates an account. The administrator comes from the server's
  // environment; everyone else is created by that administrator.
  try {
    const state = await (await fetch('/api/auth/state')).json();
    const note = el('bootstrap-note');
    note.classList.toggle('hidden', state.admin_configured);
    if (!state.admin_configured) {
      note.innerHTML = '<strong>No administrator is configured.</strong> Set '
        + '<code>SIGNALOPS_ADMIN_EMAIL</code> and <code>SIGNALOPS_ADMIN_PASSWORD</code> '
        + 'where the server runs, then restart.';
    }
  } catch { /* the login form still works without this */ }
}

async function doLogin(event) {
  event.preventDefault();
  const status = el('login-status');
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
  }
}

async function doLogout() {
  await fetch('/api/auth/logout', { method: 'POST' });
  principal = null;
  el('app').classList.add('hidden');
  el('login-screen').classList.remove('hidden');
  el('login-status').textContent = '';
  await loadAuthState();
}

function openPasswordDialog() {
  showDialog('Change your password', `
    <p class="dialog-note">Your current password is required even though you are signed
      in — an unattended browser is the common case, and re-asking is what stops it
      becoming an account takeover.</p>
    <label for="pw-current">Current password</label>
    <input id="pw-current" class="draft-field" type="password" autocomplete="current-password" />
    <label for="pw-new">New password</label>
    <input id="pw-new" class="draft-field" type="password" autocomplete="new-password" />
    <p class="field-hint">At least 10 characters.</p>
    <p class="row-actions">
      <button class="button" onclick="savePassword()">Change password</button>
      <button class="button ghost" onclick="closeDialog()">Cancel</button></p>`);
}

async function savePassword() {
  const response = await fetch('/api/auth/password', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ current_password: el('pw-current').value,
                           new_password: el('pw-new').value }),
  });
  if (!response.ok) return toast((await response.json()).detail || 'could not change', false);
  closeDialog();
  toast('Password changed');
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
  audit: renderAudit,
  agents: renderAgents,
  workflows: renderWorkflows,
  runs: renderRuns,
  approvals: renderApprovals,
  connections: renderConnections,
  users: renderUsers,
};

function switchView(view) {
  currentView = view;
  el('view-title').textContent = VIEW_TITLES[view] || view;
  renderNav();
  const render = VIEW_RENDERERS[view];
  return render ? render() : renderPlaceholder(view);
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
    <p class="agent-purpose">SignalOps reads credentials from the environment and never
      stores them. There is no field here to type a password into — set these where the
      server runs, then test.</p>
    ${rows}
    <p class="field-hint">Reads need the instance URL and the read account. Writing work
      notes needs the write account as well; without it the workflow still runs and records
      what it would have written.</p>
    <p class="row-actions">
      <button class="button ghost" onclick="testConnection()">Test connection</button>
    </p>
    <p id="conn-result" class="field-hint"></p>
    ${wizardNav('Back', 'Next', 'wizardNext()', {
      note: data.missing_for_reads.length
        ? 'You can continue without credentials — the workflow will run on tickets you paste in.'
        : '' })}`;
}

async function testConnection() {
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

async function renderWorkflows() {
  el('view').innerHTML = '<div class="empty">Loading workflows…</div>';
  let data;
  try {
    data = await (await fetch('/api/workflows')).json();
  } catch {
    el('view').innerHTML = '<div class="empty">Workflows could not be loaded.</div>';
    return;
  }
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
    <div class="agent-item">
      <div class="agent-top">
        <strong>${esc(w.name)}</strong>
        <span class="tier-badge">${esc(w.template.replace(/_/g, ' '))}</span>
        <span class="tier-badge ${w.enabled ? 'ok' : ''}">${w.enabled ? 'enabled' : 'not enabled'}</span>
        ${w.config.dry_run ? '<span class="tier-badge">dry run</span>' : ''}
        ${w.polling ? '<span class="tier-badge warn">polling</span>' : ''}
      </div>
      <p class="agent-purpose">Budget $${(w.config.run_budget_usd ?? 1).toFixed(2)} per run.
        ${w.config.dry_run ? 'External writes are recorded, not sent.'
          : 'External writes are live.'}
        ${w.tested ? '' : ' Never test-run.'}</p>
      <div class="row-actions">
        <button class="button ghost" onclick="resumeWizard('${w.id}')">Set up</button>
        <button class="button" onclick="openRunDialog('${w.id}')" ${canRun ? '' : 'disabled'}
          title="${canRun ? 'Start a run' : 'Starting a run requires the operator role'}">
          Start a run</button>
        <button class="button ghost" onclick="exportWorkflow('${w.id}')"
          ${w.exportable ? '' : 'disabled'}>Download standalone app</button>
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

async function renderRuns() {
  el('view').innerHTML = '<div class="empty">Loading runs…</div>';
  let data;
  try {
    data = await (await fetch('/api/runs')).json();
  } catch {
    el('view').innerHTML = '<div class="empty">Runs could not be loaded.</div>';
    return;
  }
  el('view').innerHTML = data.runs.length ? `
    <div class="pipeline-note">Every step is recorded as it happens, so a run that fails
      part way is still legible afterwards rather than a gap.</div>
    ${data.runs.map(runRow).join('')}`
    : '<div class="empty">No runs yet. Start one from Workflows.</div>';
}

function runRow(run) {
  const status = RUN_STATUS_CLASS[run.status] || '';
  return `
    <div class="agent-item">
      <div class="agent-top">
        <strong class="incident-id">${esc(run.trigger_ref || run.id.slice(0, 8))}</strong>
        <span class="tier-badge ${status}">${esc(run.status.replace(/_/g, ' '))}</span>
        ${run.dry_run ? '<span class="tier-badge">dry run</span>' : ''}
        <span class="audit-time">${new Date(run.started_at * 1000).toLocaleString()}</span>
      </div>
      ${run.error ? `<p class="agent-purpose">${esc(run.error)}</p>` : ''}
      <div class="row-actions">
        <button class="button ghost" onclick="showRun('${run.id}')">Timeline</button>
        <span class="dialog-note">$${(run.cost_usd || 0).toFixed(4)}</span>
      </div>
    </div>`;
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

async function renderApprovals() {
  el('view').innerHTML = '<div class="empty">Loading approvals…</div>';
  let data;
  try {
    data = await (await fetch('/api/approvals')).json();
  } catch {
    el('view').innerHTML = '<div class="empty">Approvals could not be loaded.</div>';
    return;
  }
  el('view').innerHTML = `
    <div class="pipeline-note">
      An approval is bound to a hash of the exact plan it was shown. If the plan changes
      afterwards the decision does not carry over — you are asked again rather than having
      approved something you never saw.
      ${data.can_decide ? '' : `<br /><strong>You are signed in as ${esc(principal.role)}</strong>,
        which can see this queue but not act on it. An admin can change your role under Users.`}
    </div>
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

function approvalCard(approval, canDecide) {
  const copy = GATE_COPY[approval.node] || GATE_COPY.gate;
  const plan = (approval.payload && approval.payload.plan) || {};
  const diagnosis = (approval.payload && approval.payload.diagnosis) || {};
  const steps = plan.steps || [];
  const awaitingReport = approval.node === 'hand_off';
  return `
    <div class="agent-item">
      <div class="agent-top">
        <strong>${esc(approval.summary)}</strong>
        ${awaitingReport ? '<span class="tier-badge warn">awaiting your report</span>' : ''}
        ${approval.payload && approval.payload.simulated
          ? '<span class="tier-badge warn">simulated</span>' : ''}
      </div>
      ${diagnosis.root_cause ? `<p class="agent-purpose"><strong>Cause:</strong>
        ${esc(diagnosis.root_cause)}
        (${Math.round((diagnosis.confidence || 0) * 100)}% confident)</p>` : ''}
      ${awaitingReport ? `<p class="agent-purpose">This plan was approved. Run the steps,
        then say what happened — the ticket is only resolved if you report success.</p>` : ''}
      <ol class="plan-steps">
        ${steps.map(step => `<li><strong>${esc(step.action || '')}</strong>
          <br /><span class="dialog-note">verify: ${esc(step.verify || '')}</span>
          <br /><span class="dialog-note">rollback: ${esc(step.rollback || '')}</span></li>`).join('')}
      </ol>
      <p class="dialog-note">Pinned to ${esc(approval.payload_hash.slice(0, 12))}…</p>
      <div class="row-actions">
        <button class="button" onclick="decide('${approval.id}', true, '${approval.node}')"
          ${canDecide ? '' : 'disabled'}
          title="${canDecide ? esc(copy.ask) : 'Deciding requires the approver role'}">
          ${esc(copy.yes)}</button>
        <button class="button ghost" onclick="decide('${approval.id}', false, '${approval.node}')"
          ${canDecide ? '' : 'disabled'}>${esc(copy.no)}</button>
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
      summary of it. You can change the model, rewrite the prompt, set thresholds and
      enable or disable each one. <strong>Tools and access tier are defined in code</strong>
      and stay fixed: customisation changes how an agent judges, never what it can reach.
    </div>
    ${canEdit ? '' : `<div class="pipeline-note" style="border-color:var(--warn)">
      <strong>You are acting as ${esc(principal.role)}, so editing is read-only.</strong>
      Agent configuration is an admin action. Switch your role to <em>admin</em> in the
      sidebar to edit — the login is a placeholder, so you can change it freely.
    </div>`}
    <p><button class="button ghost" onclick="exportAllAgents()">Download all agents (.zip)</button></p>
    <div class="agent-grid">
      ${agentCache.map(agentCard).join('')}
    </div>`;
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

async function renderConnections() {
  el('view').innerHTML = '<div class="empty">Loading…</div>';
  let data;
  try {
    data = await (await fetch('/api/connections')).json();
  } catch {
    el('view').innerHTML = '<div class="empty">Connections could not be loaded.</div>';
    return;
  }
  connectionCache = data.connections;
  const isAdmin = principal.role === 'admin';
  const canTest = principal.role !== 'viewer';
  el('view').innerHTML = `
    <div class="pipeline-note">
      One connection is one ServiceNow instance with one account. Add as many as you
      need — a dev instance and a production one are simply two connections, and each
      workflow names the one it uses. Credentials are <strong>encrypted before they are
      stored</strong> and no screen or endpoint can read them back.
    </div>
    <div class="row-actions">
      <button class="button" onclick="openConnectionDialog()" ${isAdmin ? '' : 'disabled'}
        title="${isAdmin ? 'Add a ServiceNow instance' : 'Adding a connection requires admin'}">
        Add ServiceNow connection</button>
    </div>
    ${data.connections.length
      ? data.connections.map(c => connectionCard(c, isAdmin, canTest)).join('')
      : '<div class="empty">No connections yet. Add one to point a workflow at ServiceNow.</div>'}
    ${data.environment_usable ? `<div class="pipeline-note">Environment variables are also
      configured (<code>${esc(data.environment_auth_method)}</code> auth). A workflow with no
      connection selected falls back to them, so an older setup keeps working.</div>` : ''}`;
}

function connectionCard(c, isAdmin, canTest) {
  const tested = c.last_tested_at
    ? `${c.last_test_ok ? 'ok' : 'failing'} · ${new Date(c.last_tested_at * 1000).toLocaleString()}`
    : 'never tested';
  return `
    <div class="agent-item">
      <div class="agent-top">
        <strong>${esc(c.name)}</strong>
        <span class="tier-badge">${esc(c.auth_type)} auth</span>
        <span class="tier-badge ${c.last_test_ok === true ? 'ok' : c.last_test_ok === false ? 'bad' : ''}">
          ${esc(tested)}</span>
        ${c.assignment_group ? `<span class="tier-badge warn">queue: ${esc(c.assignment_group)}</span>` : ''}
      </div>
      <p class="agent-purpose"><code>${esc(c.base_url)}</code> as <code>${esc(c.username)}</code></p>
      ${c.assignment_group
        ? `<p class="field-hint">Polls incidents assigned to <strong>${esc(c.assignment_group)}</strong>.</p>`
        : '<p class="field-hint">No queue set — this connection will not trigger runs on its own.</p>'}
      ${c.last_test_detail ? `<p class="field-hint">${esc(c.last_test_detail)}</p>` : ''}
      <div class="row-actions">
        <button class="button ghost" onclick="testConnection('${c.id}')" ${canTest ? '' : 'disabled'}>
          Test connection</button>
        <button class="button ghost" onclick="openConnectionDialog('${c.id}')" ${isAdmin ? '' : 'disabled'}>
          Edit</button>
        <button class="button ghost" onclick="deleteConnection('${c.id}')" ${isAdmin ? '' : 'disabled'}>
          Delete</button>
      </div>
      <p class="field-hint" id="conn-result-${c.id}"></p>
    </div>`;
}

function openConnectionDialog(id) {
  const c = connectionCache.find(x => x.id === id) || {};
  const editing = Boolean(id);
  showDialog(editing ? `Edit ${c.name}` : 'Add ServiceNow connection', `
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

    <p class="row-actions">
      <button class="button" onclick="saveConnection(${editing ? `'${id}'` : 'null'})">
        ${editing ? 'Save changes' : 'Create connection'}</button>
      <button class="button ghost" onclick="closeDialog()">Cancel</button></p>`);
}

function toggleOauthFields() {
  el('cn-oauth').classList.toggle('hidden', el('cn-auth').value !== 'oauth');
}

async function saveConnection(id) {
  const body = {
    kind: 'servicenow',
    name: el('cn-name').value.trim(),
    base_url: el('cn-url').value.trim(),
    auth_type: el('cn-auth').value,
    username: el('cn-user').value.trim(),
    password: el('cn-pass').value || null,
    client_id: el('cn-cid') ? el('cn-cid').value.trim() : '',
    client_secret: el('cn-csec') ? (el('cn-csec').value || null) : null,
    assignment_group: el('cn-queue').value.trim(),
    extra_query: el('cn-extra').value.trim(),
  };
  if (!body.name || !body.base_url) return toast('Name and instance URL are required', false);
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

async function testConnection(id) {
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

async function renderUsers() {
  el('view').innerHTML = '<div class="empty">Loading…</div>';
  if (principal.role !== 'admin') {
    el('view').innerHTML = `<div class="pipeline-note">Managing users requires the admin
      role. You are signed in as <strong>${esc(principal.role)}</strong>.</div>`;
    return;
  }
  const data = await (await fetch('/api/users')).json();
  el('view').innerHTML = `
    <div class="pipeline-note">
      Roles are enforced by the server on every request, not by hiding buttons.
      <strong>Viewer</strong> reads. <strong>Operator</strong> starts runs.
      <strong>Approver</strong> decides on human gates. <strong>Admin</strong> manages
      agents, connections, users and the kill switch.
    </div>
    <div class="row-actions">
      <button class="button" onclick="openUserDialog()">Invite user</button>
    </div>
    ${data.users.map(u => `
      <div class="agent-item">
        <div class="agent-top">
          <strong>${esc(u.display_name)}</strong>
          <span class="tier-badge ${u.role === 'admin' ? 'warn' : ''}">${esc(u.role)}</span>
          ${u.active ? '' : '<span class="tier-badge bad">deactivated</span>'}
          ${u.locked ? '<span class="tier-badge bad">locked</span>' : ''}
          ${u.must_change_password ? '<span class="tier-badge">must change password</span>' : ''}
        </div>
        <p class="agent-purpose"><code>${esc(u.email)}</code>${u.last_login_at
          ? ` · last signed in ${new Date(u.last_login_at * 1000).toLocaleString()}`
          : ' · never signed in'}</p>
        <div class="row-actions">
          <button class="button ghost" onclick="openUserDialog('${u.id}')">Edit</button>
        </div>
      </div>`).join('')}`;
  window.__users = data.users;
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

async function saveUser(id) {
  const body = id
    ? { display_name: el('us-name').value.trim(), role: el('us-role').value,
        active: el('us-active') ? el('us-active').checked : undefined,
        password: el('us-pass').value || undefined }
    : { email: el('us-email').value.trim(), display_name: el('us-name').value.trim(),
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

(async function boot() {
  try {
    const response = await fetch('/api/auth/me');
    if (response.ok) { principal = await response.json(); return showApp(); }
  } catch { /* fall through to the login gate */ }
  await loadAuthState();
  el('login-screen').classList.remove('hidden');
})();
