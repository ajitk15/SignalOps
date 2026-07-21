# Common MQ / ACE Failure Patterns

Reference for the Diagnostician agent. Not exhaustive — extend with patterns
specific to your real environment as you learn from real incidents.

## Queue depth climbing steadily
- **Consumer app down or hung**: the app that reads this queue has crashed,
  deadlocked, or lost its connection. Depth rises because production
  continues but consumption stops.
- **Downstream dependency outage**: the consumer is up but blocked waiting on
  a downstream system (DB, API) it depends on, so it isn't pulling messages.
- **ACE flow deployment failure**: a recent ACE flow deploy failed or the
  flow is in a stopped state, so nothing is draining the queue.

## Messages landing on a DLQ
- **Poison message**: a malformed message the consumer can't parse gets
  rejected repeatedly and routed to the DLQ.
- **Message format/version mismatch**: an upstream app changed its message
  schema without the consumer being updated.
- **Backout threshold exceeded**: the queue's BOTHRESH was hit after retries,
  so MQ moved the message to the DLQ automatically.

## Channel not RUNNING
- **Network partition**: connectivity lost between queue managers (common
  for cluster/receiver channels).
- **Partner queue manager down**: the QM on the other end of the channel is
  stopped or unreachable.
- **Channel stopped by an operator or a previous failure**: check channel
  status detail for STOPPED vs RETRYING vs INACTIVE — each implies a
  different next step.

## Storage/disk related
- **Queue manager log or data filesystem full**: causes broad failures
  across many queues on the same QM at once — if multiple unrelated queues
  on the same QM are anomalous simultaneously, suspect this before a
  per-queue cause.
