# Standalone SignalOps workflow.
#
#   docker build -t signalops-workflow .
#   docker run --rm -it -e ANTHROPIC_API_KEY=sk-... \
#       -v "$PWD/checkpoints:/app/checkpoints" \
#       signalops-workflow sample_ticket.json
#
# -it matters: the workflow pauses at a human gate and reads your answer from
# stdin. Without a TTY the approval prompt has nobody to ask and the run stops.
FROM python:3.12-slim

# Dependencies first so edits to the workflow or the agent prompts do not
# invalidate the pip layer.
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The checkpoint database is the run's durability. Keep it on a volume, or a
# run interrupted mid-approval cannot be resumed after the container exits.
ENV CHECKPOINT_DIR=/app/checkpoints
VOLUME ["/app/checkpoints"]

# Never bake a key into an image. Pass it at run time with -e or --env-file.
ENV ANTHROPIC_API_KEY=""

# Non-root: this workflow only ever reads its own directory.
RUN useradd --create-home --uid 10001 runner \
    && mkdir -p /app/checkpoints && chown -R runner:runner /app
USER runner

ENTRYPOINT ["python", "workflow.py"]
CMD ["sample_ticket.json"]
