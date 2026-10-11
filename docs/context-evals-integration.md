# context-evals integration contract

This repository owns execution. A future `context-evals` repository should be
an observer, not another executor.

## Event stream

Set `CTX_EVAL_EVENTS_PATH` to opt into an append-only JSONL stream:

```bash
export CTX_EVAL_EVENTS_PATH="$XDG_STATE_HOME/ctx-agent/eval-events.jsonl"
```

Each line is a schema-versioned event produced by
`ctx_agent.eval_events.build_eval_event`. The initial contract includes:

- plan lifecycle events such as creation, approval, rejection, cancellation,
  completion, and internal failure;
- decision requests when the agent asks the owner for a deviation;
- decision resolutions when the owner approves or rejects that request;
- a source field distinguishing local and remote execution.

The stream intentionally excludes raw audit details. Decision request prose,
credentials, model transcripts, shell output, and internal failure text remain
inside the authoritative agent stores. The evaluator receives identifiers and
outcomes, which are enough to calculate behavioral metrics without becoming a
second sensitive log archive.

## Trust boundary

`context-evals` should have no route for approving deviations, mutating plans,
issuing shell commands, or writing coordinator state. Its first consumers can
therefore be implemented safely as offline/read-only features:

1. **Shadow Mode** records what an evaluator would have predicted at each
   owner-decision point, then compares that prediction to the owner's actual
   choice.
2. **Decision/outcome scoring** joins `decision.requested`,
   `decision.resolved`, and later plan outcomes by `scope_id` and
   `decision_id`.
3. **Autonomy evidence** measures how often the agent finishes work without an
   owner interruption, how often requests are accepted, and where failures
   occur.
4. **Performance reviews** aggregate the above over a project or time window.
5. **Minion School / Synthetic Boss** can eventually replay historical event
   sequences in simulation, but must remain disconnected from live execution.

## Schema evolution

Consumers must reject unknown major schema versions and tolerate unknown event
kinds within a supported version. New fields should be additive. Sensitive raw
details should not be added to this stream; use opaque identifiers and derived
measurements instead.

The producer is best-effort. If the export path is missing or unwritable, agent
execution continues normally. Evaluation is telemetry, never a dependency.
