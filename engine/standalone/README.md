# {{WORKFLOW_NAME}} — standalone

Exported from SignalOps on {{EXPORTED_AT}}. This is a self-contained Python app
that runs the same workflow the platform runs: the same graph, the same agents,
the same prompts, the same human gate.

```
{{GRAPH}}
```

---

## Setup

Python 3.11 or newer. Check with `python --version`.

### 1. Create a virtual environment

A virtual environment keeps these dependencies out of your system Python.

**macOS / Linux**

```bash
python3 -m venv .venv
source .venv/bin/activate
```

**Windows (PowerShell)**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If PowerShell refuses with an execution-policy error, run
`Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` in that session and
try again.

**Windows (cmd)**

```
python -m venv .venv
.venv\Scripts\activate.bat
```

Your prompt should now start with `(.venv)`. Everything below assumes it is
active — if you open a new terminal, activate it again.

### 2. Install the dependencies

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### 3. Provide an API key

```bash
cp .env.example .env
```

Edit `.env` and set `ANTHROPIC_API_KEY` to a key from
<https://console.anthropic.com/settings/keys>. The file is read by your shell,
not by the app, so export it:

**macOS / Linux**

```bash
set -a && . ./.env && set +a
```

**Windows (PowerShell)**

```powershell
Get-Content .env | Where-Object { $_ -match '=' -and $_ -notmatch '^\s*#' } |
  ForEach-Object { $p = $_ -split '=', 2; [Environment]::SetEnvironmentVariable($p[0].Trim(), $p[1].Trim()) }
```

Never commit the filled-in `.env`.

### 4. Run it

```bash
python workflow.py sample_ticket.json
```

The run prints each agent as it is called, prints the work note it would post,
then pauses and asks you to approve the plan. Answer `y` or `n`.

To run against a real incident, write a JSON file in the shape of
`sample_ticket.json` and pass that instead.

---

## Docker

If you would rather not manage a virtual environment:

```bash
docker build -t signalops-workflow .
docker run --rm -it \
  -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
  -v "$PWD/checkpoints:/app/checkpoints" \
  signalops-workflow sample_ticket.json
```

Two flags carry weight. **`-it`** gives the container a terminal; the workflow
pauses for approval and without a TTY there is nobody to ask. **`-v`** keeps the
checkpoint database outside the container, so a run paused at the gate survives
the container exiting.

Never bake the key into the image — pass it with `-e` or `--env-file .env`.

---

## Resuming

Every run is checkpointed after each step, so an interrupted run picks up where
it stopped instead of starting over — and paying again for the steps that
already completed. The last line of every run prints the command:

```bash
python workflow.py sample_ticket.json --thread <run-id>
```

---

## Configuration

| Setting | Where | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | env | Required. |
| `ALWAYS_ASK` | env or `--always-ask` | Pause for approval on every run. Default **true**. |
| `CONFIDENCE_THRESHOLD` | env or `--threshold` | With `ALWAYS_ASK=false`, plans at or above this confidence skip the gate. Default 0.8. |
| Agent model, prompt | `agents/*.md` | Frontmatter sets the model; the body is the system prompt. |

Leave `ALWAYS_ASK=true` until you have watched enough runs to trust the
threshold. It is the only thing standing between a low-confidence plan and an
operator acting on it.

### Changing an agent

`agents/` holds one file per agent in the Claude subagent format — YAML
frontmatter, then the system prompt. Edit the body to change how an agent
reasons; change `model:` to `haiku`, `sonnet` or `opus` to trade cost against
capability. No code change is needed.

**Keep the safety preamble** at the top of each prompt. It is what makes ticket
text data rather than instructions. A ticket description is attacker-influenced
in most organisations, and this workflow feeds ticket text straight to a model.

---

## What did not come with it

This is the honest part of a lift-and-shift. The platform did more than run the
graph, and those parts are not in this directory:

| Not included | Consequence |
|---|---|
| **Run history and audit trail** | Nothing records who approved what. Only the checkpoint database persists, and it is state, not evidence. |
| **Roles and permissions** | Whoever can run this can approve its plans. |
| **Tool tier enforcement** | The `tools:` line in each agent file is documentation here. Nothing stops an agent you rewire from reaching further. |
| **Ticketing integration** | The work note is printed, not posted. There is no ServiceNow or Jira client. |
| **Cost budget** | Cost is reported at the end, not capped during. A pathological run is not stopped. |
| **Kill switch** | There is nothing to halt a fleet of these; you stop them one terminal at a time. |
| **Concurrency control** | One ticket per invocation. |

The workflow logic is faithful. The governance around it is not, and the gap is
mostly what a platform is for. Use this to run the workflow somewhere the
platform cannot go, to review exactly what the agents are asked, or as the
starting point for an integration of your own — not as a drop-in replacement for
a governed deployment.

---

## Files

| File | What it is |
|---|---|
| `workflow.py` | The graph, the model calls, the CLI. The whole app. |
| `schemas.py` | Output schemas. A reply that does not fit fails the step rather than being patched up. |
| `agents/*.md` | One agent each: model, tools, system prompt. |
| `agents_config.json` | What was exported, for reference — the running config comes from the `.md` files. |
| `sample_ticket.json` | Input shape. |
| `requirements.txt`, `Dockerfile`, `.env.example` | Setup. |
| `checkpoints.db` | Created on first run. Delete to forget every run. |
