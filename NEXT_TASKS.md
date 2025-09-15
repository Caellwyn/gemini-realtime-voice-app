# NEXT_TASKS Backlog (Agent & Dev Oriented)

This file tracks forward-looking work. Keep entries concise, actionable, and prunable. When a task is completed, remove or move to a lightweight CHANGELOG (future optional).

## Priority Bands

* P1: High leverage / stability / correctness
* P2: User experience & performance improvements
* P3: Nice-to-have / exploratory

## P1 (Immediate / High Value)

1. Debug Filled PDF Validation
   * Add a utility to reconstruct a dict from the generated filled PDF (where possible) or re-run fill in dry-run mode and diff against in-memory `session_manager` state.
   * Output: JSON diff (missing, mismatched, truncated) + optional log entry.
   * Acceptance: Running the validator after confirmation prints zero mismatches on a known-good sample.
2. Graceful Shutdown Improvements
   * Drain active WebSocket sessions: stop accepting new, await tool response flush, send final notice.
   * Add structured shutdown summary (active sessions closed, duration, pending tasks canceled) to stdout.
   * Acceptance: `Ctrl+C` exits without noisy stack traces and logs a single summary line.
3. Field Name Normalization Pass
   * Implement normalization pipeline: strip underscores, collapse multiple spaces, remove trailing numeric artifacts (except true disambiguators), infer human-readable labels from nearby PDF text objects (future scaffolding).
   * Acceptance: Example PDF shows cleaner field names in UI; mapping preserved for fill.

## P2 (Experience / Performance)

1. Checkbox / Radio / Combo Detection
   * Extend `pdf_form/extract.py` to classify widget types; store type metadata in schema.
   * Acceptance: A sample PDF with a checkbox yields type info; model system prompt could later adapt.
2. Health Endpoint `/healthz`
   * Lightweight JSON: `{status:"ok", uptime_secs, active_sessions}`.
   * Acceptance: Returns 200 during normal run; used by future monitors.
3. Structured Logging Option
   * Add config switch to output JSON logs (tool calls, errors) for aggregation.

## P3 (Exploratory / Later)

1. Multi-Page Navigation Support
   * Provide metadata for fields beyond page 1; optional UI pagination.
2. Field Type Validation Heuristics
   * Regex or simple classifiers (e.g., date, phone) to gently nudge model (tool response hints).
3. Output Flatten / Audit Variant
   * Option to produce a flattened PDF + a summary JSON or second page of captured values.
4. Adaptive Clarification Strategy
   * Track repeated misunderstandings; escalate to state summary before re-asking a field.

## Technical Notes / Hooks

* Direct sync path reduces race window; validator should operate on in-memory state just before fill.
* Shutdown hook location: `app.py` signal handler + an added `ConnectionManager.shutdown()` coroutine.
* Normalization pipeline can be placed in `pdf_form/extract.py` near where field names are canonicalized.

## Fast Start Checklist (Next Session)

* [ ] Implement validator skeleton (reuse fill mapping; compare dictionaries)
* [ ] Add shutdown drain scaffold (mark accepting=False, gather tasks)
* [ ] Prototype normalization function + apply to extracted names

Last Updated: Sept 15, 2025
