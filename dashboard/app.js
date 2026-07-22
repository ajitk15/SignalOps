const incidentIds = new Set();

function el(id) { return document.getElementById(id); }
function formatTime(timestamp) {
  return timestamp ? new Date(timestamp * 1000).toLocaleString() : 'time unavailable';
}

// Relative times everywhere: "32s ago" reads instantly where a clock time
// forces mental arithmetic. Absolute time lives in the hover title.
function relTime(timestamp) {
  const seconds = Math.max(0, Date.now() / 1000 - timestamp);
  if (seconds < 60) return `${Math.round(seconds)}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}
function relSpan(timestamp, prefix = '') {
  return `<span data-ts="${timestamp}" data-prefix="${prefix}" title="${formatTime(timestamp)}">${prefix}${relTime(timestamp)}</span>`;
}
setInterval(() => document.querySelectorAll('[data-ts]').forEach(node => {
  node.textContent = node.dataset.prefix + relTime(+node.dataset.ts);
}), 10000);

// Errors surface as a toast, not a silently frozen page.
let toastTimer;
function toast(message) {
  const node = el('toast');
  node.textContent = message;
  node.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.remove('show'), 6000);
}

function dismissNote() {
  localStorage.setItem('noteDismissed', '1');
  el('pipeline-note').style.display = 'none';
}
if (localStorage.getItem('noteDismissed')) {
  document.addEventListener('DOMContentLoaded', () => el('pipeline-note').style.display = 'none');
}

// status is 'ok' | 'anomaly' | 'unknown'; timestamp is when the reading was
// taken (epoch seconds), not when this tile happened to be rendered — a tile
// rebuilt from replayed event history is not a fresh observation.
// Tiles are keyed by source AND name: two sources may watch same-named objects.
function renderTile(source, objectName, objectType, status, timestamp) {
  const container = el('tiles');
  container.querySelectorAll('.empty').forEach(e => e.remove());
  const tileId = 'tile-' + source + ':' + objectName;
  let node = document.getElementById(tileId);
  if (!node) {
    node = document.createElement('div');
    node.className = 'tile';
    node.id = tileId;
    container.appendChild(node);
  }
  const dotClass = status === 'anomaly' ? 'warn' : status === 'ok' ? 'ok' : 'unknown';
  node.classList.toggle('anomaly', status === 'anomaly');
  node.innerHTML = `<div class="type">${esc(objectType)} · ${esc(source)}</div>
<div class="name"><span class="dot ${dotClass}"></span>${esc(objectName)}</div>
<div class="detail">${timestamp ? relSpan(timestamp, 'updated ') : 'awaiting first poll'}</div>`;
  // Anomalies cluster at the front so trouble is never below the fold.
  if (status === 'anomaly') container.prepend(node); else container.appendChild(node);
}

// --- pipeline strip: stages appear as their events arrive, so a new agent
// in the backend never crashes the dashboard — it just gets a chip. -----
const stages = {};
const STAGE_LABELS = { watcher: 'Collection & rules', diagnostician: 'Diagnostician', report_writer: 'Report writer' };

function stageChip(name) {
  if (stages[name]) return stages[name];
  const chip = document.createElement('div');
  chip.className = 'stage-chip';
  chip.id = 'stage-' + name;
  const isAi = name !== 'watcher';
  chip.innerHTML = `<div class="stage-name"><span>${esc(STAGE_LABELS[name] || name)}</span>
    <span class="mode ${isAi ? 'ai' : 'rules'}">${isAi ? 'AI' : 'No AI'}</span></div>
  <div class="stage-state">idle</div><div class="stage-cost">$0.00000 · 0 calls</div>`;
  el('strip').appendChild(chip);
  stages[name] = { node: chip, cost: 0, calls: 0 };
  return stages[name];
}

function setStage(name, state, { active = false, cost, model } = {}) {
  const stage = stageChip(name);
  stage.node.classList.toggle('active', active);
  stage.node.querySelector('.stage-state').textContent = state;
  if (cost !== undefined) {
    stage.cost += (cost || 0);
    stage.calls += 1;
    stage.node.querySelector('.stage-cost').textContent =
      `$${stage.cost.toFixed(5)} · ${stage.calls} calls` + (model ? ` · ${model}` : '');
  }
  // Mirror onto the Agents-tab workflow card, if the roster rendered one.
  const card = document.getElementById('wf-' + name);
  if (card) {
    card.classList.toggle('running', active);
    card.querySelector('.agent-state').textContent = state;
    card.querySelector('.agent-cost').textContent = `$${stage.cost.toFixed(5)} · ${stage.calls} calls`;
  }
}

// --- Agents tab: the whole workflow, visible whether or not it has run ---
let pipelineInfo = null;

function renderWorkflow() {
  if (!pipelineInfo) return;
  const container = el('workflow');
  container.replaceChildren();
  const aiOn = pipelineInfo.ai_enabled;
  pipelineInfo.stages.forEach((stage, index) => {
    if (index > 0) {
      const arrow = document.createElement('div');
      arrow.className = 'flow-arrow';
      arrow.textContent = '→';
      container.appendChild(arrow);
    }
    // The cost gate sits between deterministic collection and the AI agents.
    if (index === 1) {
      const gate = document.createElement('div');
      gate.className = 'gate-card';
      gate.innerHTML = `<h4>AI gate — all must hold</h4><ul>
        <li>New incident (not a duplicate within the dedup window)</li>
        <li>Severity at least ${esc(pipelineInfo.minimum_ai_severity)}</li>
        <li>No approved KB match ≥ ${Math.round(pipelineInfo.kb_reuse_threshold * 100)}%</li>
        <li class="${aiOn ? 'gate-on' : 'gate-off'}">ENABLE_INCIDENT_AI is ${aiOn ? 'on ✓' : 'off ✗'}</li>
      </ul>`;
      container.appendChild(gate);
      const arrow = document.createElement('div');
      arrow.className = 'flow-arrow';
      arrow.textContent = '→';
      container.appendChild(arrow);
    }
    const card = document.createElement('div');
    card.className = 'agent-card' + (stage.ai && !aiOn ? ' disabled' : '');
    card.id = 'wf-' + stage.name;
    const idleText = stage.ai
      ? (aiOn ? 'idle — runs for eligible new incidents' : 'AI disabled — set ENABLE_INCIDENT_AI=true')
      : 'waiting for collection';
    card.innerHTML = `<h3>${esc(stage.label)}<span class="mode ${stage.ai ? 'ai' : 'rules'}">${stage.ai ? 'AI' : 'No AI'}</span></h3>
      ${stage.model ? `<div class="agent-model">${esc(stage.model)}</div>` : ''}
      <div class="agent-role">${esc(stage.role || '')}</div>
      <div class="agent-state">${idleText}</div>
      <div class="agent-cost">$0.00000 · 0 calls</div>`;
    container.appendChild(card);
  });
}

function prependIncident(evt, live = false) {
  const id = evt.payload.incident_id ?? evt.payload.id;
  if (!id || incidentIds.has(id)) return;
  incidentIds.add(id);
  const container = el('incidents');
  // The empty-state element is a direct child of the incident container.
  // Remove only that message, not the container itself.
  container.querySelectorAll('.empty').forEach(e => e.remove());
  const row = document.createElement('div');
  const severity = evt.payload.severity || 'P4';
  const status = evt.payload.status || 'open';
  row.className = 'incident-row';
  row.dataset.severity = severity;
  row.dataset.status = status;
  row.id = 'incident-row-' + id;
  const trigger = triggerKind(evt.payload.trigger_source);
  const createdAt = evt.payload.created_at ?? evt.ts;
  row.innerHTML = `<span>
  <span class="incident-id">#${esc(id)}</span>
  <span class="trigger-badge trigger-${esc(trigger)}">${trigger === 'event' ? '⚡ event' : 'poll'}</span>
  ${esc(evt.payload.title || evt.payload.object_name)}
  ${evt.payload.external_refs?.servicenow?.number ? `<span class="trigger-badge trigger-event">SNOW ${esc(evt.payload.external_refs.servicenow.number)}</span>` : ''}
  ${evt.payload.reopen_count ? `<span class="trigger-badge trigger-event">reopened ×${esc(evt.payload.reopen_count)}</span>` : ''}
  <span class="incident-time">${relSpan(createdAt, 'Generated: ')}</span>
</span>
<span><span class="status-badge status-${esc(status)}">${esc(status.replace('_', ' '))}</span>
<span class="sev sev-${esc(severity)}">${esc(evt.payload.severity || '—')}</span></span>`;
  row.onclick = () => loadIncident(id);
  applyRowFilter(row);
  // A serious incident arriving live should catch the eye, once.
  if (live && (severity === 'P1' || severity === 'P2')) row.classList.add('flash');
  container.prepend(row);
  updateSummary();
}

// --- severity summary + filters ------------------------------------
let activeFilter = null;
// Default view is live work: finished incidents are hidden until asked for.
let statusFilter = 'active';
const ACTIVE_STATUSES = ['open', 'acknowledged'];

function rowIsVisible(row) {
  if (activeFilter && row.dataset.severity !== activeFilter) return false;
  if (statusFilter === 'active') return ACTIVE_STATUSES.includes(row.dataset.status);
  if (statusFilter === 'all') return true;
  return row.dataset.status === statusFilter;
}

function applyRowFilter(row) {
  row.classList.toggle('filtered', !rowIsVisible(row));
}

function updateSummary() {
  const rows = [...document.querySelectorAll('#incidents .incident-row')];
  const counts = { P1: 0, P2: 0, P3: 0, P4: 0 };
  let open = 0;
  rows.forEach(row => {
    if (counts[row.dataset.severity] !== undefined) counts[row.dataset.severity]++;
    if (ACTIVE_STATUSES.includes(row.dataset.status)) open++;
  });
  el('summary').innerHTML = `<span class="pill p1">P1 ${counts.P1}</span><span class="pill p2">P2 ${counts.P2}</span>
    <span class="pill">P3 ${counts.P3}</span><span class="pill">P4 ${counts.P4}</span>
    <span class="pill">open ${open}</span><span class="pill" id="mttr-pill"></span>`;
  loadMetrics();
}

async function loadMetrics() {
  try {
    const metrics = await (await fetch('/api/metrics')).json();
    const pill = el('mttr-pill');
    if (!pill) return;
    pill.textContent = metrics.mttr_seconds ? `MTTR ${relDuration(metrics.mttr_seconds)}` : 'MTTR n/a';
  } catch { /* summary still renders without it */ }
}

function relDuration(seconds) {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `${(seconds / 3600).toFixed(1)}h`;
  return `${(seconds / 86400).toFixed(1)}d`;
}

function renderFilters() {
  el('filters').innerHTML = ['P1', 'P2', 'P3', 'P4'].map(sev =>
    `<button class="chip ${activeFilter === sev ? 'on' : ''}" onclick="setFilter('${sev}')">${sev}</button>`).join('')
    + `<button class="chip ${activeFilter === null ? 'on' : ''}" onclick="setFilter(null)">All sev</button>`
    + '<span class="chip-sep"></span>'
    + [['active', 'Active'], ['resolved', 'Resolved'], ['closed', 'Closed'], ['all', 'All']].map(([value, label]) =>
      `<button class="chip ${statusFilter === value ? 'on' : ''}" onclick="setStatusFilter('${value}')">${label}</button>`).join('');
}

function refilter() {
  renderFilters();
  document.querySelectorAll('#incidents .incident-row').forEach(applyRowFilter);
}

function setFilter(severity) { activeFilter = severity; refilter(); }
function setStatusFilter(status) { statusFilter = status; refilter(); }

async function loadIncident(id) {
  const res = await fetch('/api/incidents/' + id);
  const data = await res.json();
  if (data.error) return;
  const trigger = triggerKind(data.trigger_source);
  const route = data.report_json?.route;
  const diagnosisAiStatus = route === 'ai' ? 'AI used' : route === 'ai_failed_rule_only' ? 'AI failed' : 'AI not used';
  const reportAiStatus = route === 'ai' ? 'AI used' : route === 'ai_failed_rule_only' ? 'Fallback report' : 'Rule / KB result';
  const diagnosisMode = route === 'ai_failed_rule_only' ? 'failed' : 'ai';
  el('detail').innerHTML = `
<h3><span class="incident-id">Incident #${esc(data.id)}</span>${esc(data.title)} <span class="sev sev-${esc(data.severity)}">${esc(data.severity)}</span>
  <span class="status-badge status-${esc(data.status || 'open')}">${esc((data.status || 'open').replace('_', ' '))}</span>
  <span class="trigger-badge trigger-${esc(trigger)}">${trigger === 'event' ? '⚡ event' : 'poll'}</span>
  ${data.reopen_count ? `<span class="trigger-badge trigger-event">reopened ×${esc(data.reopen_count)}</span>` : ''}</h3>
<p class="incident-summary">${esc(data.object_name)} (${esc(data.object_type)}) · Generated: ${formatTime(data.created_at)} · $${(data.total_cost_usd || 0).toFixed(5)} total
  ${data.previous_incident_id ? `· recurrence of <a href="#" onclick="loadIncident(${esc(data.previous_incident_id)});return false">#${esc(data.previous_incident_id)}</a>` : ''}
  ${data.assignee ? `· assigned to ${esc(data.assignee)}` : ''}</p>
${renderLifecycleActions(data)}
<section class="stage"><div class="stage-header"><div><div class="stage-title">1. Collection</div><div class="stage-subtitle">Live MCP observation and deterministic rules</div></div><span class="mode rules">No AI</span></div><div class="stage-body">${renderDetails(data.watcher_json)}</div></section>
<section class="stage"><div class="stage-header"><div><div class="stage-title">2. Diagnostician</div><div class="stage-subtitle">Root-cause investigation</div></div><span class="mode ${diagnosisMode}">${diagnosisAiStatus}</span></div><div class="stage-body">${renderDetails(data.diagnosis_json)}</div></section>
<section class="stage"><div class="stage-header"><div><div class="stage-title">3. Report</div><div class="stage-subtitle">Ticket-ready summary and next steps</div></div><span class="mode ${diagnosisMode}">${reportAiStatus}</span></div><div class="stage-body kb-rendered">${renderMarkdown(data.markdown_report || '')}</div></section>
<div class="kb-section"><h4>Knowledge base</h4><div class="kb-actions"><button class="button" onclick="openKbDraft(${id})">Create KB draft</button></div><div id="kb-articles" class="kb-list"><span class="empty">Loading relevant approved articles…</span></div></div>
<div class="kb-section"><h4>Audit trail</h4>
  <div class="audit-list">${data.audit?.length ? renderAuditRows(data.audit) : '<span class="empty">No recorded actions yet.</span>'}</div>
</div>
  `;
  loadKbArticles(id);
}

// --- incident lifecycle --------------------------------------------
const NEXT_ACTIONS = {
  open: [['acknowledged', 'Acknowledge', ''], ['resolved', 'Resolve', ''], ['false_positive', 'False positive', 'danger']],
  acknowledged: [['resolved', 'Resolve', ''], ['false_positive', 'False positive', 'danger']],
  resolved: [['closed', 'Close', ''], ['open', 'Reopen', '']],
  closed: [['open', 'Reopen', '']],
  false_positive: [['open', 'Reopen', '']],
};

function renderLifecycleActions(data) {
  const status = data.status || 'open';
  const actions = NEXT_ACTIONS[status] || [];
  return `<div class="lifecycle">
    <input id="lifecycle-actor" class="draft-field lifecycle-actor" placeholder="Your name or team"
           value="${esc(localStorage.getItem('signalops-actor') || '')}" />
    <input id="lifecycle-note" class="draft-field lifecycle-note" placeholder="Resolution note (optional)" />
    <div class="row-actions">${actions.map(([next, label, kind]) =>
      `<button class="button ${kind}" onclick="setIncidentStatus(${data.id}, '${next}')">${label}</button>`).join('')}</div>
  </div>`;
}

async function setIncidentStatus(id, status) {
  const actor = el('lifecycle-actor').value.trim();
  if (!actor) { toast('Enter your name — it is recorded in the audit trail'); return; }
  localStorage.setItem('signalops-actor', actor);
  const note = el('lifecycle-note').value.trim();
  try {
    const response = await fetch(`/api/incidents/${id}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status, actor, note: note || null })
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || 'update failed');
    toast(`Incident #${id} ${status.replace('_', ' ')}`
      + (result.servicenow ? ` · ServiceNow ${result.servicenow}` : ''));
    loadIncident(id);
    // Resolving is the moment the human knows the answer — ask for the KB
    // article now rather than hoping they remember later.
    if (status === 'resolved') openKbDraft(id);
  } catch (error) { toast(String(error.message)); }
}

function applyIncidentUpdate(payload) {
  const row = document.getElementById('incident-row-' + payload.incident_id);
  if (!row) return;
  row.dataset.status = payload.status;
  const badge = row.querySelector('.status-badge');
  if (badge) {
    badge.className = `status-badge status-${payload.status}`;
    badge.textContent = payload.status.replace('_', ' ');
  }
  applyRowFilter(row);
  updateSummary();
}

async function loadKbArticles(id) {
  const container = el('kb-articles');
  try {
    const articles = await (await fetch(`/api/incidents/${id}/kb-articles`)).json();
    container.replaceChildren();
    if (!articles.length) {
      container.innerHTML = '<span class="empty">No relevant approved KB articles were found.</span>';
      return;
    }
    for (const article of articles) {
      const button = document.createElement('button');
      button.className = 'kb-article';
      const title = document.createElement('span'); title.textContent = article.title;
      const score = document.createElement('span'); score.className = 'kb-score'; score.textContent = `${Math.round(article.score * 100)}% match · Open`;
      button.append(title, score);
      button.onclick = () => showKbArticle(article);
      container.appendChild(button);
    }
  } catch {
    container.innerHTML = '<span class="empty">KB articles could not be loaded.</span>';
  }
}

function showKbArticle(article) {
  el('kb-title').textContent = article.title;
  el('kb-content').innerHTML = renderMarkdown(article.content);
  el('kb-dialog').showModal();
}

function escapeHtml(value) {
  return value.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// Every value below reaches us from MQ output or an agent, so none of it is
// trusted markup. The String() coercion matters: escapeHtml calls .replace,
// which throws on the undefined fields some event payloads omit.
function esc(value) {
  return escapeHtml(String(value ?? ''));
}

// Incidents store the raw observation source (mq_mcp, splunk, …). Only the
// two badge styles exist, so collapse sources to the one that describes how
// the signal reached us: monitoring webhooks push, MCP collection polls.
const EVENT_SOURCES = new Set(['splunk', 'dynatrace']);
function triggerKind(source) {
  return EVENT_SOURCES.has(source) ? 'event' : 'poll';
}

function readableKey(key) {
  return key.replace(/_/g, ' ').replace(/\b\w/g, char => char.toUpperCase());
}

function renderFactValue(value) {
  if (Array.isArray(value)) return `<ul>${value.map(item => `<li>${renderFactValue(item)}</li>`).join('')}</ul>`;
  if (value && typeof value === 'object') return `<dl class="facts">${Object.entries(value).map(([key, item]) => `<dt>${escapeHtml(readableKey(key))}</dt><dd>${renderFactValue(item)}</dd>`).join('')}</dl>`;
  return escapeHtml(value === null || value === undefined || value === '' ? '—' : String(value));
}

function renderDetails(value) {
  if (!value || typeof value !== 'object') return `<span class="empty">No details recorded.</span>`;
  return `<dl class="facts">${Object.entries(value).map(([key, item]) => `<dt>${escapeHtml(readableKey(key))}</dt><dd>${renderFactValue(item)}</dd>`).join('')}</dl>`;
}

function renderInline(value) {
  return escapeHtml(value)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\*([^*]+)\*/g, '<em>$1</em>');
}

function renderMarkdown(markdown) {
  const lines = markdown.replace(/<!--[\s\S]*?-->/g, '').replace(/\r/g, '').split('\n');
  const html = []; let listType = null; let inCode = false; let code = [];
  const closeList = () => { if (listType) { html.push(`</${listType}>`); listType = null; } };
  for (const line of lines) {
    if (line.startsWith('```')) {
      if (inCode) { html.push(`<pre><code>${escapeHtml(code.join('\n'))}</code></pre>`); code = []; }
      else closeList();
      inCode = !inCode; continue;
    }
    if (inCode) { code.push(line); continue; }
    const heading = line.match(/^(#{1,3})\s+(.+)$/);
    const bullet = line.match(/^[-*]\s+(.+)$/);
    const numbered = line.match(/^\d+\.\s+(.+)$/);
    if (heading) { closeList(); const level = heading[1].length; html.push(`<h${level}>${renderInline(heading[2])}</h${level}>`); }
    else if (bullet || numbered) {
      const type = bullet ? 'ul' : 'ol';
      if (listType && listType !== type) closeList();
      if (!listType) { html.push(`<${type}>`); listType = type; }
      html.push(`<li>${renderInline((bullet || numbered)[1])}</li>`);
    } else if (!line.trim()) { closeList(); }
    else { closeList(); html.push(`<p>${renderInline(line)}</p>`); }
  }
  if (inCode) html.push(`<pre><code>${escapeHtml(code.join('\n'))}</code></pre>`);
  closeList();
  return html.join('');
}

async function openKbDraft(id) {
  const status = el('kb-draft-status');
  status.textContent = 'Generating draft…';
  el('kb-draft-content').value = '';
  el('kb-draft-dialog').showModal();
  try {
    const draft = await (await fetch(`/api/incidents/${id}/kb-draft`)).json();
    el('kb-draft-content').value = draft.markdown || '';
    status.textContent = 'Review the draft, confirm the resolution, then approve it.';
    el('kb-approve-button').onclick = () => approveKbDraft(id);
  } catch {
    status.textContent = 'Unable to generate the draft.';
  }
}

async function approveKbDraft(id) {
  const approvedBy = el('kb-approved-by').value.trim();
  const markdown = el('kb-draft-content').value.trim();
  const status = el('kb-draft-status');
  if (!approvedBy || !markdown) {
    status.textContent = 'Enter the approving person or team and keep the reviewed article content.';
    return;
  }
  if (!confirm('Confirm that the Resolution is human-reviewed and approved for future incident guidance.')) return;
  status.textContent = 'Publishing approved article…';
  try {
    const response = await fetch(`/api/incidents/${id}/kb-approve`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ markdown, approved_by: approvedBy })
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || 'Approval failed');
    status.textContent = `Published: ${result.filename}`;
    loadKbArticles(id);
  } catch (error) {
    status.textContent = error.message;
  }
}

const INCIDENT_PAGE_SIZE = 100;

async function loadIncidents() {
  const incidents = await (await fetch(`/api/incidents?limit=${INCIDENT_PAGE_SIZE}`)).json();
  incidents.reverse().forEach(incident => prependIncident({ payload: incident }));
  // A full page means older incidents exist that this list is not showing.
  // Say so rather than letting the list look complete.
  // Deliberately not class="empty" — prependIncident clears those on every
  // live incident, and the truncation stays true after one arrives.
  if (incidents.length === INCIDENT_PAGE_SIZE) {
    const note = document.createElement('div');
    note.className = 'list-note';
    note.textContent = `Showing the ${INCIDENT_PAGE_SIZE} most recent incidents.`;
    el('incidents').appendChild(note);
  }
}

// A two-tone wordmark when the title ends in "Ops", plain text otherwise —
// the title is configurable, so the styling must degrade gracefully.
function renderBrand(title) {
  document.title = title;
  const node = el('app-title');
  const match = /^(.*?)(Ops)$/.exec(title);
  if (match) {
    node.replaceChildren(document.createTextNode(match[1]));
    const ops = document.createElement('span');
    ops.className = 'ops';
    ops.textContent = match[2];
    node.appendChild(ops);
  } else {
    node.textContent = title;
  }
}
renderBrand('SignalOps');

// The socket's opening frame carries current state, so the panel paints on
// load rather than waiting a whole poll interval for the next event.
function applySnapshot(payload) {
  if (payload.title) renderBrand(payload.title);
  const objects = payload.watched_objects || [];
  if (objects.length) {
    objects.forEach(o => renderTile(o.source, o.object_name, o.object_type, o.status, o.timestamp));
  } else if (payload.collector && payload.collector.status === 'degraded') {
    el('tiles').innerHTML = '<span class="empty">Collection is running but returned no readings — check that the systems behind the collectors are reachable.</span>';
  } else {
    el('tiles').innerHTML = '<span class="empty">No readings yet — the first collection cycle lands within one poll interval.</span>';
  }
  // Seed every configured stage so agents are visible before any event.
  if (payload.pipeline) {
    pipelineInfo = payload.pipeline;
    pipelineInfo.stages.forEach(stage => { STAGE_LABELS[stage.name] = stage.label; });
    renderWorkflow();
    pipelineInfo.stages.forEach(stage => {
      stageChip(stage.name);
      const idle = stage.ai
        ? (pipelineInfo.ai_enabled ? 'idle — runs for eligible new incidents' : 'AI disabled')
        : 'waiting for collection';
      if (!stages[stage.name].calls) setStage(stage.name, idle);
    });
  } else {
    stageChip('watcher');
  }
  renderCollectorHealth(payload.collector);
}

// Collection can die independently of this socket, so "live" alone would be
// a misleading green light. One labelled pill rather than anonymous dots —
// a bare dot tells you nothing until you hover it.
function renderCollectorHealth(collector) {
  const health = el('health');
  health.querySelectorAll('.coll').forEach(node => node.remove());
  const collectors = collector && collector.collectors ? Object.entries(collector.collectors) : [];
  if (!collectors.length) return;
  const counts = {};
  collectors.forEach(([, h]) => { counts[h.status] = (counts[h.status] || 0) + 1; });
  const total = collectors.length;
  let state = 'ok';
  let text = `${total} collector${total === 1 ? '' : 's'} ok`;
  if (counts.failing) {
    state = 'failing';
    text = `${counts.failing}/${total} failing · retry ${collector.next_attempt_in ?? '?'}s`;
  } else if (counts.degraded) {
    state = 'degraded';
    text = `${counts.degraded}/${total} degraded · no readings`;
  } else if (!counts.ok) {
    state = 'starting';
    text = 'collectors starting…';
  }

  const pill = document.createElement('button');
  pill.className = `status-pill coll ${state}`;
  pill.innerHTML = `<span class="health-dot ${state}"></span>${esc(text)}`;
  // Per-collector detail stays available without occupying header width.
  pill.title = collectors.map(([name, h]) =>
    `${name}: ${h.status}`
    + (h.last_error ? ` — ${h.last_error}` : '')
    + (h.status === 'failing' ? ` (retry in ${h.next_attempt_in}s)` : '')).join('\n')
    + '\n\nClick for integration details';
  pill.onclick = () => switchView('integrations');
  health.appendChild(pill);

  // Stale tiles must not keep implying their readings are current.
  if (counts.failing) {
    document.querySelectorAll('#tiles .tile .dot').forEach(dot => dot.className = 'dot unknown');
  }
}

const VIEWS = ['dashboard', 'agents', 'rules', 'integrations', 'kb', 'audit'];
function switchView(view) {
  VIEWS.forEach(name => el(name + '-view').classList.toggle('hidden', name !== view));
  document.querySelectorAll('.tab').forEach(tab => tab.classList.toggle('active', tab.dataset.view === view));
  if (view === 'kb') loadKbLibrary();
  if (view === 'rules') loadRules();
  if (view === 'integrations') loadIntegrations();
  if (view === 'audit') loadAudit();
}

// --- Audit tab -----------------------------------------------------
const AUDIT_LABELS = {
  incident_open: 'reopened', incident_acknowledged: 'acknowledged', incident_resolved: 'resolved',
  incident_closed: 'closed', incident_false_positive: 'marked false positive',
  kb_approved: 'approved KB article', kb_edited: 'edited KB article', kb_deleted: 'deleted KB article',
  rule_created: 'created rule', rule_updated: 'modified rule', rule_deleted: 'deleted rule',
  rule_disabled: 'disabled rule', rule_reset: 'reset rule',
};

function renderAuditRows(entries) {
  return entries.map(entry => `<div class="audit-row">
    <span class="audit-actor">${esc(entry.actor)}</span>
    <span>${esc(AUDIT_LABELS[entry.action] || entry.action)}</span>
    <span class="incident-id">${esc(entry.entity_type)} ${esc(entry.entity_id)}</span>
    <span class="audit-time">${relSpan(entry.ts)}</span>
    ${entry.detail?.note ? `<div class="audit-note">“${esc(entry.detail.note)}”</div>` : ''}
  </div>`).join('');
}

async function loadAudit() {
  const container = el('audit-list');
  try {
    const data = await (await fetch('/api/audit?limit=200')).json();
    container.innerHTML = data.entries.length
      ? renderAuditRows(data.entries)
      : '<span class="empty">No recorded actions yet.</span>';
  } catch { container.innerHTML = '<span class="empty">Audit trail could not be loaded.</span>'; }
}

// --- Rules tab -----------------------------------------------------
let rulePlatforms = [];
let platformFilter = 'all';

// Original functional glyphs, deliberately NOT the vendors' trademarked
// logos: each depicts what the platform does, so it stays legally clean and
// still reads at 18px. Inline SVG keeps the no-asset, CSP-clean story.
const PLATFORM_MARKS = {
  // queued messages draining into a pipe
  ibm_mq: `<rect x="2" y="5" width="4" height="4" rx="1"/><rect x="7.5" y="5" width="4" height="4" rx="1"/>
           <rect x="13" y="5" width="4" height="4" rx="1"/><path d="M2 14h13a3 3 0 0 0 3-3" fill="none" stroke-width="2"/>`,
  // branching integration flow
  ibm_ace: `<circle cx="4" cy="10" r="2.5"/><circle cx="16" cy="4.5" r="2.5"/><circle cx="16" cy="15.5" r="2.5"/>
            <path d="M6.5 10h3l4-4.5M9.5 10h0l4 4.5" fill="none" stroke-width="1.8"/>`,
  // requests passing through a gateway arch
  apigee: `<path d="M4 16V9a6 6 0 0 1 12 0v7" fill="none" stroke-width="2"/><path d="M2 16h16" stroke-width="2"/>
           <path d="M7 11h6M11 8.5l2.5 2.5L11 13.5" fill="none" stroke-width="1.6"/>`,
  // appliance chip with a power bolt
  datapower: `<rect x="4" y="4" width="12" height="12" rx="2" fill="none" stroke-width="2"/>
              <path d="M10.8 6.6 8 10.6h2.2L9.2 13.8 12.4 9.6h-2.3z"/><path d="M7 2v2M13 2v2M7 16v2M13 16v2" stroke-width="1.6"/>`,
  // steam rising from a cup
  java: `<path d="M4 9h9v3.5a3.5 3.5 0 0 1-3.5 3.5H7.5A3.5 3.5 0 0 1 4 12.5z" fill="none" stroke-width="1.8"/>
         <path d="M13 10h1.6a1.9 1.9 0 0 1 0 3.8H13" fill="none" stroke-width="1.6"/>
         <path d="M6.5 2.5c1.5 1.2-1.5 2.3 0 3.5M10 2.5c1.5 1.2-1.5 2.3 0 3.5" fill="none" stroke-width="1.5"/>`,
  // shared across platforms — overlapping rings
  common: `<circle cx="7.5" cy="10" r="4.5" fill="none" stroke-width="1.8"/><circle cx="12.5" cy="10" r="4.5" fill="none" stroke-width="1.8"/>`,
  other: `<circle cx="10" cy="10" r="6.5" fill="none" stroke-width="2"/><circle cx="10" cy="10" r="1.8"/>`,
};

function platformMark(id, size = 18) {
  const paths = PLATFORM_MARKS[id] || PLATFORM_MARKS.other;
  return `<svg class="platform-mark platform-${esc(id)}" viewBox="0 0 20 20" width="${size}" height="${size}"
    aria-hidden="true" stroke="currentColor" fill="currentColor" stroke-linecap="round"
    stroke-linejoin="round">${paths}</svg>`;
}

function platformName(id) {
  if (id === 'common') return 'Common (cross-platform)';
  return rulePlatforms.find(p => p.id === id)?.name || 'Other';
}

function ruleSummary(rule) {
  const c = rule.condition || {};
  if (c.type === 'greater_than') return `${rule.when?.metric ?? ''} > ${c.value === '${threshold}' ? 'per-target threshold' : c.value}${rule.escalate ? ` (≥×${rule.escalate.at_factor} → ${rule.escalate.severity})` : ''}`;
  if (c.type === 'not_in') return `${rule.when?.metric ?? ''} not in [${(c.values || []).join(', ')}]`;
  if (c.type === 'rising') return `${rule.when?.metric ?? ''} strictly rising`;
  return c.type || '';
}

// Severity may be fixed, AI-decided, or unset (defaults to P3).
function severityBadge(rule) {
  if (rule.severity === 'ai') {
    const provisional = rule.ai_provisional || 'P3';
    return `<span class="sev sev-${esc(provisional)}">AI · from ${esc(provisional)}</span>`;
  }
  const severity = rule.severity || 'P3';
  return `<span class="sev sev-${esc(severity)}">${esc(severity)}${rule.severity ? '' : ' · default'}</span>`;
}

let allRules = [];

function ruleRow(rule) {
  const builtin = rule.origin === 'built-in';
  const row = document.createElement('div'); row.className = 'kb-library-row';
  if (rule.disabled) row.style.opacity = '.55';
  const info = document.createElement('div');
  info.innerHTML = `<h3>${platformMark(rule.platform, 16)} ${esc(rule.id)} ${rule.disabled ? '' : severityBadge(rule)}
    <span class="trigger-badge trigger-poll">${esc(rule.origin)}</span>
    ${rule.overridden ? '<span class="trigger-badge trigger-event">modified</span>' : ''}
    ${rule.disabled ? '<span class="trigger-badge trigger-poll">disabled</span>' : ''}</h3>
    <p>${rule.disabled ? 'Disabled — not evaluated.' : esc(ruleSummary(rule)) + ' — “' + esc(rule.message) + '”'}</p>`;
  const actions = document.createElement('div'); actions.className = 'row-actions';
  if (!rule.disabled) {
    const edit = document.createElement('button');
    edit.className = 'button'; edit.textContent = 'Modify';
    edit.onclick = () => openRuleDialog(rule);
    actions.appendChild(edit);
  }
  // Built-ins are disabled rather than deleted, and reset restores the
  // shipped definition exactly — the file on disk is never rewritten.
  if (builtin && (rule.overridden || rule.disabled)) {
    const reset = document.createElement('button');
    reset.className = 'button'; reset.textContent = 'Reset';
    reset.onclick = () => resetRule(rule.id);
    actions.appendChild(reset);
  }
  if (!rule.disabled) {
    const remove = document.createElement('button');
    remove.className = 'button danger'; remove.textContent = builtin ? 'Disable' : 'Delete';
    remove.onclick = () => deleteRule(rule.id, builtin);
    actions.appendChild(remove);
  }
  row.append(info, actions);
  return row;
}

function renderPlatformTabs() {
  const counts = {};
  allRules.forEach(rule => { counts[rule.platform] = (counts[rule.platform] || 0) + 1; });
  const active = allRules.filter(rule => !rule.disabled).length;
  const tabs = [`<button class="ptab ${platformFilter === 'all' ? 'on' : ''}" onclick="setPlatformFilter('all')">
      All active rules <span class="ptab-count">${active}</span></button>`];
  // Only offer a platform tab when something is actually there — empty tabs
  // are noise.
  for (const platform of rulePlatforms) {
    if (!counts[platform.id]) continue;
    tabs.push(`<button class="ptab ${platformFilter === platform.id ? 'on' : ''}"
      onclick="setPlatformFilter('${esc(platform.id)}')">${platformMark(platform.id)}
      ${esc(platform.name)} <span class="ptab-count">${counts[platform.id]}</span></button>`);
  }
  // Cross-platform and unattributed rules still need a home.
  for (const extra of ['common', 'other']) {
    if (!counts[extra]) continue;
    tabs.push(`<button class="ptab ${platformFilter === extra ? 'on' : ''}" onclick="setPlatformFilter('${extra}')">
      ${platformMark(extra)} ${extra === 'common' ? 'Common' : 'Other'}
      <span class="ptab-count">${counts[extra]}</span></button>`);
  }
  el('platform-tabs').innerHTML = tabs.join('');
}

function renderRuleList() {
  const container = el('rules-list');
  container.replaceChildren();
  if (platformFilter === 'all') {
    // "All active rules" means exactly that: disabled ones live under their
    // platform tab, where you go to re-enable them.
    const active = allRules.filter(rule => !rule.disabled);
    active.forEach(rule => container.appendChild(ruleRow(rule)));
    if (!active.length) container.innerHTML = '<span class="empty">No active rules.</span>';
    return;
  }
  const rules = allRules.filter(rule => rule.platform === platformFilter);
  const heading = document.createElement('h3');
  heading.className = 'platform-heading';
  heading.innerHTML = `${platformMark(platformFilter, 22)} ${esc(platformName(platformFilter))}`;
  container.appendChild(heading);
  rules.forEach(rule => container.appendChild(ruleRow(rule)));
  if (!rules.length) container.innerHTML = '<span class="empty">No rules for this platform yet.</span>';
}

function setPlatformFilter(id) {
  platformFilter = id;
  renderPlatformTabs();
  renderRuleList();
}

async function loadRules() {
  try {
    const data = await (await fetch('/api/rules')).json();
    rulePlatforms = data.platforms || [];
    allRules = [...data.builtin, ...data.custom];
    renderPlatformTabs();
    renderRuleList();
  } catch { el('rules-list').innerHTML = '<span class="empty">Rules could not be loaded.</span>'; }
}

let editingRuleId = null;
let selectedPlatform = null;

function toggleProvisional() {
  el('provisional-row').classList.toggle('hidden', el('rule-severity').value !== 'ai');
}

function openRuleDialog(rule = null) {
  editingRuleId = rule ? rule.id : null;
  // Editing must not drop a rule's platform attribution; a fresh rule
  // starts unattributed until a template is chosen.
  selectedPlatform = rule && rule.platform !== 'other' ? rule.platform : null;
  const select = el('rule-template');
  // Grouped by platform, with the category carried in the option text so
  // the "what should I monitor for X" question is answered in one list.
  select.innerHTML = '<option value="">— from scratch —</option>' + rulePlatforms.map((platform, pi) =>
    `<optgroup label="${esc(platform.name)}">` + platform.templates.map((tpl, ti) =>
      `<option value="${pi}:${ti}">${esc(tpl.category)} · ${esc(tpl.label)}</option>`).join('') + '</optgroup>').join('');
  el('rule-dialog-title').textContent = rule ? `Modify rule: ${rule.id}` : 'Add detection rule';
  el('rule-save').textContent = rule ? 'Save changes' : 'Create rule';
  // The id is the identity of an override — changing it would orphan the
  // edit and resurrect the original built-in.
  el('rule-id').readOnly = Boolean(rule);
  el('rule-template-row').classList.toggle('hidden', Boolean(rule));
  if (rule) {
    el('rule-id').value = rule.id;
    el('rule-metric').value = rule.when?.metric ?? '';
    el('rule-condition').value = rule.condition?.type ?? 'greater_than';
    el('rule-value').value = rule.condition?.type === 'not_in'
      ? (rule.condition.values || []).join(', ')
      : (rule.condition?.default ?? rule.condition?.value ?? '');
    el('rule-severity').value = rule.severity ?? '';
    el('rule-provisional').value = rule.ai_provisional ?? 'P3';
    el('rule-message').value = rule.message ?? '';
    el('rule-status').textContent = rule.origin === 'built-in'
      ? 'Editing a built-in rule saves an override; Reset restores the shipped version.' : '';
  } else {
    ['rule-id', 'rule-metric', 'rule-value', 'rule-message'].forEach(id => el(id).value = '');
    el('rule-severity').value = 'P2';
    el('rule-status').textContent = '';
  }
  toggleProvisional();
  el('rule-dialog').showModal();
}

async function resetRule(id) {
  if (!confirm(`Reset "${id}" to its shipped definition?`)) return;
  const response = await fetch(`/api/rules/${id}/reset`, { method: 'POST' });
  if (!response.ok) { toast('Reset failed'); return; }
  loadRules();
}

function applyRuleTemplate() {
  const value = el('rule-template').value;
  if (!value) { selectedPlatform = null; return; }
  const [pi, ti] = value.split(':').map(Number);
  const platform = rulePlatforms[pi];
  const template = platform.templates[ti];
  const rule = template.rule;
  // Remember the platform so the created rule is attributed explicitly
  // rather than relying on metric inference.
  selectedPlatform = platform.id;
  el('rule-id').value = `${platform.name} ${template.label}`.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
  el('rule-metric').value = rule.metric;
  el('rule-condition').value = rule.condition.type;
  el('rule-value').value = rule.condition.type === 'not_in' ? (rule.condition.values || []).join(', ')
    : (rule.condition.default ?? rule.condition.value ?? '');
  el('rule-severity').value = rule.severity;
  el('rule-message').value = rule.message;
}

async function saveRule() {
  const type = el('rule-condition').value;
  const rawValue = el('rule-value').value.trim();
  const condition = { type };
  if (type === 'greater_than') {
    // Per-target thresholds stay overridable; the entered number is the default.
    condition.value = '${threshold}';
    condition.default = Number(rawValue) || 0;
  } else if (type === 'not_in') {
    condition.values = rawValue.split(',').map(v => v.trim()).filter(Boolean);
  }
  const severity = el('rule-severity').value;
  const body = { id: el('rule-id').value.trim(), metric: el('rule-metric').value.trim(),
    condition, message: el('rule-message').value.trim() };
  if (severity) body.severity = severity;
  if (severity === 'ai') body.ai_provisional = el('rule-provisional').value;
  if (selectedPlatform) body.platform = selectedPlatform;
  const status = el('rule-status');
  status.textContent = editingRuleId ? 'Saving…' : 'Creating…';
  try {
    const response = await fetch(editingRuleId ? `/api/rules/${editingRuleId}` : '/api/rules', {
      method: editingRuleId ? 'PUT' : 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail?.[0]?.msg || result.detail || 'save failed');
    status.textContent = (editingRuleId ? 'Rule saved' : 'Rule created')
      + ' — active for future observations (trend history resets).';
    loadRules();
  } catch (error) { status.textContent = String(error.message); }
}

async function deleteRule(id, builtin) {
  const message = builtin
    ? `Disable built-in rule "${id}"? It stops being evaluated; Reset restores it.`
    : `Delete custom rule "${id}"?`;
  if (!confirm(message)) return;
  const response = await fetch(`/api/rules/${id}`, { method: 'DELETE' });
  if (!response.ok) { toast(builtin ? 'Disable failed' : 'Delete failed'); return; }
  loadRules();
}

// --- Integrations tab ----------------------------------------------
async function loadIntegrations() {
  const container = el('integrations-list');
  try {
    const integrations = await (await fetch('/api/integrations')).json();
    container.replaceChildren();
    for (const integration of integrations) {
      const row = document.createElement('div'); row.className = 'kb-library-row';
      const info = document.createElement('div');
      const statusDot = integration.configured
        ? '<span class="health-dot ok" style="margin-right:6px"></span>'
        : '<span class="health-dot starting" style="margin-right:6px"></span>';
      info.innerHTML = `<h3>${statusDot}${esc(integration.name)}
          ${integration.mode ? `<span class="trigger-badge trigger-poll">${esc(integration.mode)}</span>` : ''}</h3>
        <p>${esc(integration.purpose)}</p>
        <p>${integration.configured ? 'Configured via environment.' : 'Not configured — set: '}
          ${integration.env.map(v => `<code>${esc(v)}</code>`).join(' ')}</p>`;
      const actions = document.createElement('div'); actions.className = 'row-actions';
      const test = document.createElement('button');
      test.className = 'button'; test.textContent = 'Test';
      test.onclick = async () => {
        test.disabled = true; test.textContent = 'Testing…';
        try {
          const result = await (await fetch(`/api/integrations/${integration.key}/test`, { method: 'POST' })).json();
          toast(`${integration.name}: ${result.ok ? 'connection ok' : result.error}`);
        } catch { toast(`${integration.name}: test failed`); }
        test.disabled = false; test.textContent = 'Test';
      };
      actions.appendChild(test); row.append(info, actions); container.appendChild(row);
    }
  } catch { container.innerHTML = '<span class="empty">Integrations could not be loaded.</span>'; }
}

async function loadKbLibrary() {
  const container = el('kb-library');
  try {
    const articles = await (await fetch('/api/kb-articles')).json();
    container.replaceChildren();
    if (!articles.length) { container.innerHTML = '<span class="empty">No approved KB articles yet.</span>'; return; }
    for (const article of articles) {
      const row = document.createElement('div'); row.className = 'kb-library-row';
      const info = document.createElement('div');
      const title = document.createElement('h3'); title.textContent = article.title;
      if (article.servicenow) {
        const badge = document.createElement('span');
        badge.className = 'trigger-badge trigger-event';
        badge.style.marginLeft = '8px';
        badge.textContent = article.servicenow.number ? `SNOW ${article.servicenow.number}` : 'SNOW dry-run';
        title.appendChild(badge);
      }
      const updated = document.createElement('p'); updated.textContent = `Last updated: ${formatTime(article.updated_at)}`;
      info.append(title, updated);
      const actions = document.createElement('div'); actions.className = 'row-actions';
      const view = document.createElement('button'); view.className = 'button'; view.textContent = 'View'; view.onclick = () => showKbArticle(article);
      const edit = document.createElement('button'); edit.className = 'button'; edit.textContent = 'Edit'; edit.onclick = () => openKbEditor(article);
      const remove = document.createElement('button'); remove.className = 'button danger'; remove.textContent = 'Delete'; remove.onclick = () => deleteKbArticle(article);
      actions.append(view, edit, remove); row.append(info, actions); container.appendChild(row);
    }
  } catch { container.innerHTML = '<span class="empty">KB articles could not be loaded.</span>'; }
}

function openKbEditor(article) {
  el('kb-editor-title').textContent = `Edit: ${article.title}`;
  el('kb-editor-content').value = article.content.replace(/<!--[\s\S]*?-->\s*/g, '');
  el('kb-editor-status').textContent = 'Review all changes before saving.';
  el('kb-editor-dialog').showModal();
  el('kb-save-button').onclick = () => saveKbArticle(article.slug);
}

async function saveKbArticle(slug) {
  const editedBy = el('kb-edited-by').value.trim(); const markdown = el('kb-editor-content').value.trim(); const status = el('kb-editor-status');
  if (!editedBy || !markdown) { status.textContent = 'Enter the editor and retain article content.'; return; }
  if (!confirm('Confirm these changes were reviewed and are approved for future incident guidance.')) return;
  const response = await fetch(`/api/kb-articles/${slug}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ markdown, edited_by: editedBy }) });
  const result = await response.json(); status.textContent = response.ok ? 'Saved.' : (result.detail || 'Save failed');
  if (response.ok) loadKbLibrary();
}

async function deleteKbArticle(article) {
  if (!confirm(`Delete approved KB article "${article.title}"? This cannot be undone.`)) return;
  const response = await fetch(`/api/kb-articles/${article.slug}`, { method: 'DELETE' }); const result = await response.json();
  if (!response.ok) { alert(result.detail || 'Delete failed'); return; }
  loadKbLibrary();
}

function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/ws/events`);
  ws.onopen = () => { el('conn').textContent = 'live'; el('conn').className = 'conn live'; };
  ws.onclose = () => { el('conn').textContent = 'disconnected — retrying…'; el('conn').className = 'conn down'; setTimeout(connect, 2000); };
  ws.onerror = () => ws.close();
  // One bad event must not silently kill every event after it.
  ws.onmessage = (msg) => {
    try {
      handleEvent(JSON.parse(msg.data));
    } catch (error) {
      console.error('event handling failed', error);
      toast(`Dashboard event error: ${error.message}`);
    }
  };
}

function handleEvent(evt) {
  switch (evt.type) {
    case 'state_snapshot':
      applySnapshot(evt.payload);
      break;
    case 'collector_status':
      renderCollectorHealth(evt.payload);
      break;
    case 'observation_received': {
      // Collection is rule-based: each normalized observation is stage
      // activity with no model call or cost attached.
      const observation = evt.payload.observation;
      const isAnomalous = Boolean(evt.payload.finding);
      renderTile(observation.source, observation.object_name, observation.object_type, isAnomalous ? 'anomaly' : 'ok', observation.timestamp);
      setStage('watcher', `Last: ${observation.object_name} (${isAnomalous ? 'anomaly' : 'ok'})`);
      break;
    }
    case 'agent_started':
      setStage(evt.payload.agent, `Working on ${evt.payload.object_name}…`, { active: true });
      break;
    case 'agent_completed':
      setStage(evt.payload.agent, `Done: ${evt.payload.object_name}`,
        { cost: evt.payload.cost_usd, model: evt.payload.model });
      break;
    case 'agent_failed':
      setStage(evt.payload.agent, `Failed: ${evt.payload.object_name}`);
      break;
    case 'incident_created':
      prependIncident(evt, true);
      break;
    case 'incident_updated':
      applyIncidentUpdate(evt.payload);
      if (evt.payload.reopened) toast(`Incident #${evt.payload.incident_id} reopened by recurrence`);
      break;
  }
}

connect();
loadIncidents().catch(() => toast('Could not load the incident list.'));
renderFilters();
updateSummary();
