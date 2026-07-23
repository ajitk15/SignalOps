# SignalAIOps — production image.
#
# Ships the FastAPI app, the Python dependencies, and — because the implementer
# agent runs on the Claude Agent SDK, which shells out to the `claude` binary —
# Node.js and the Claude Code CLI. git is present for the ticket-to-PR workflow's
# throwaway clones.
#
#   docker compose up -d --build
#
# See docs/deploy-vps.md for the full Hostinger/Ubuntu walkthrough.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    NODE_MAJOR=20

# System dependencies:
#   git                — repo clones for the ticket-to-PR workflow
#   curl, ca-certs, gnupg — fetch the NodeSource key and packages
#   build-essential, libffi-dev — fallback to build argon2-cffi / cffi wheels
#     on architectures without a prebuilt wheel (e.g. arm64 VPS)
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates gnupg build-essential libffi-dev \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update && apt-get install -y --no-install-recommends nodejs \
    # The Claude Code CLI the Agent SDK spawns as a subprocess.
    && npm install -g @anthropic-ai/claude-code \
    && apt-get purge -y gnupg && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first, so application edits don't invalidate the pip layer.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Run as a non-root user with a real, writable home — the claude CLI keeps its
# config under $HOME, and the app writes its database, encryption key and
# checkpoints under /app/data.
RUN useradd --create-home --uid 10001 signalops \
    && mkdir -p /app/data \
    && chown -R signalops:signalops /app
USER signalops
ENV HOME=/home/signalops

# The database, the generated encryption key and the run checkpoints all live
# here. Mount a volume so they survive a container replacement.
VOLUME ["/app/data"]

EXPOSE 8000

# A failed boot (e.g. no admin configured) should show as unhealthy, not as a
# container that looks up but serves nothing.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health').status==200 else 1)" || exit 1

CMD ["uvicorn", "server.app:app", "--host", "0.0.0.0", "--port", "8000"]
