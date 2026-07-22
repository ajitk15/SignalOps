// SignalOps v2 — Phase 0a placeholder shell.
// Deliberately minimal: the real client is built from Phase 0b onward. This
// exists so the branch is never in a state where the server serves a broken
// page referencing endpoints that no longer exist.

const el = (id) => document.getElementById(id);

const PHASES = [
  ['0a', 'Strip the monitoring pipeline', 'in progress'],
  ['0b', 'Data model, dummy login, roles, new shell', 'next'],
  ['1', 'Agent catalogue with safe customisation', 'planned'],
  ['2', 'LangGraph workflow engine, approvals, budgets', 'planned'],
  ['3', 'Onboarding wizard + incident remediation workflow', 'planned'],
  ['4', 'Ticket → PR workflow', 'planned'],
  ['5', 'Guardrail hardening', 'planned'],
];

function renderPhases() {
  el('phases').innerHTML = PHASES.map(([id, name, state]) => `
    <div class="kb-library-row">
      <div>
        <h3><span class="incident-id">Phase ${id}</span> ${name}</h3>
      </div>
      <div class="row-actions">
        <span class="status-badge ${state === 'in progress' ? 'status-open' : ''}">${state}</span>
      </div>
    </div>`).join('');
}

async function showBuildState() {
  const pill = el('build-state');
  try {
    const health = await (await fetch('/api/health')).json();
    pill.textContent = `${health.status} · phase ${health.phase} · ${health.env}`;
  } catch {
    pill.textContent = 'server unreachable';
    pill.classList.add('failing');
  }
}

renderPhases();
showBuildState();
