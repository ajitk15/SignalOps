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

const ROLES = ['viewer', 'operator', 'approver', 'admin'];

function renderWho() {
  if (!principal) return;
  // The login is a placeholder, so the role picker belongs here too — otherwise
  // an operator hits admin-only screens with no way forward and the feature
  // just looks missing.
  el('who').innerHTML = `
    <div class="who-name">${esc(principal.display_name)}</div>
    <div class="who-role">${esc(principal.workspace.name)}</div>
    <label class="who-role-label" for="role-switch">Acting as</label>
    <select id="role-switch" class="draft-field who-role-select"
            onchange="switchRole(this.value)">
      ${ROLES.map(r => `<option value="${r}" ${r === principal.role ? 'selected' : ''}>${r}</option>`).join('')}
    </select>
    ${principal.identity_verified ? ''
      : '<div class="who-unverified" title="The login is a placeholder; this identity was not verified">unverified identity</div>'}`;
}

async function switchRole(role) {
  const response = await fetch('/api/auth/login', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ display_name: principal.display_name, role }),
  });
  if (!response.ok) { toast('Could not switch role'); return; }
  principal = await response.json();
  renderWho();
  switchView(currentView);
  toast(`Now acting as ${role}`);
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
};

function switchView(view) {
  currentView = view;
  el('view-title').textContent = VIEW_TITLES[view] || view;
  renderNav();
  const render = VIEW_RENDERERS[view];
  return render ? render() : renderPlaceholder(view);
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
        ${w.config.dry_run ? '<span class="tier-badge">dry run</span>' : ''}
      </div>
      <p class="agent-purpose">Budget $${(w.config.run_budget_usd ?? 1).toFixed(2)} per run.
        ${w.config.dry_run ? 'External writes are recorded, not sent.'
          : 'External writes are live.'}</p>
      <div class="row-actions">
        <button class="button" onclick="openRunDialog('${w.id}')" ${canRun ? '' : 'disabled'}
          title="${canRun ? 'Start a run' : 'Starting a run requires the operator role'}">
          Start a run</button>
        <button class="button ghost" onclick="exportWorkflow('${w.id}')"
          ${w.exportable ? '' : 'disabled'}>Download standalone app</button>
      </div>
    </div>`;
}

function exportWorkflow(id) { window.location = `/api/workflows/${id}/export`; }

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
        which can see this queue but not act on it. Switch to approver or admin in the sidebar.`}
    </div>
    ${data.approvals.length ? data.approvals.map(a => approvalCard(a, data.can_decide)).join('')
      : '<div class="empty">Nothing is waiting on a human.</div>'}`;
}

function approvalCard(approval, canDecide) {
  const plan = (approval.payload && approval.payload.plan) || {};
  const diagnosis = (approval.payload && approval.payload.diagnosis) || {};
  const steps = plan.steps || [];
  return `
    <div class="agent-item">
      <div class="agent-top">
        <strong>${esc(approval.summary)}</strong>
        ${approval.payload && approval.payload.simulated
          ? '<span class="tier-badge warn">simulated</span>' : ''}
      </div>
      <p class="agent-purpose"><strong>Cause:</strong> ${esc(diagnosis.root_cause || 'not established')}
        (${Math.round((diagnosis.confidence || 0) * 100)}% confident)</p>
      <ol class="plan-steps">
        ${steps.map(step => `<li><strong>${esc(step.action || '')}</strong>
          <br /><span class="dialog-note">verify: ${esc(step.verify || '')}</span>
          <br /><span class="dialog-note">rollback: ${esc(step.rollback || '')}</span></li>`).join('')}
      </ol>
      <p class="dialog-note">Pinned to ${esc(approval.payload_hash.slice(0, 12))}…</p>
      <div class="row-actions">
        <button class="button" onclick="decide('${approval.id}', true)" ${canDecide ? '' : 'disabled'}
          title="${canDecide ? 'Approve this plan' : 'Deciding requires the approver role'}">
          Approve</button>
        <button class="button ghost" onclick="decide('${approval.id}', false)"
          ${canDecide ? '' : 'disabled'}>Reject</button>
      </div>
    </div>`;
}

async function decide(approvalId, approved) {
  const note = window.prompt(approved ? 'Note (optional)' : 'Why are you rejecting it?') ?? '';
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
  el('login-screen').classList.remove('hidden');
})();
