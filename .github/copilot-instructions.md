# Copilot Instructions (Snapshot)

Purpose: Fast orientation for AI/code agents after context refresh. Human-facing docs live in `README.md`. Backlog lives in `NEXT_TASKS.md`.

## Runtime Overview
Single-process voice-driven AcroForm filler.
* `app.py` runs HTTP (upload, status, download) + WebSocket realtime session.
* Realtime path: Browser audio → WS → Gemini (function calling) → tool calls → state updates → audio/text responses.

## Active Surface
| Concern | Module(s) |
|---------|-----------|
| Startup / orchestration | `app.py` |
| Form state + system prompt | `form_manager.py` |
| Session lifecycle / expiry | `session_manager.py` |
| PDF extraction & fill | `pdf_extractor.py`, `pdf_form/` |
| Audio ingest / playback | `audio_handler.py` |
| Tool call handling | `tool_response_builder.py` |
| Connection task mgmt | `connection_manager.py` |
| Sync (direct + fallback) | `websocket_handler.PDFSyncManager` |
| Frontend UI | `index.html` |

Legacy (`main.py`, `server.py`) kept only for historical reference.

## Tools Exposed to Model
1. `update_pdf_fields` (args.updates: { field: value, ... })
2. `get_form_state`

Prompt rules (enforced in `form_manager.py`):
* Always call `update_pdf_fields` immediately when user supplies values.
* Batch multiple fields per utterance.
* Use `get_form_state` only when uncertain about remaining fields.
* After all filled → confirm → allow download.

## PDF Sync
`PDFSyncManager` auto-detects in-process direct mode and updates `SessionManager` without HTTP; falls back to POST if session not present (legacy multi-process scenario).

## Key Behaviors & Limits
* Field values truncated at 500 chars; ignore empty/whitespace.
* Extraction limited to first page; max 300 fields.
* Duplicate original names mapped via `metadata.write_name_map`.
* Completion event triggers `form_complete` then waits for user confirmation before enabling download.

## Logging Artifacts
* `tool_calls.log` – tool latency + payload slices
* `websocket_latency.log` – periodic ping RTTs
* Stdout – extraction warnings, sync fallback notices, shutdown messages

## Common Extension Hooks
* Checkbox/radio detection → extend `pdf_form/extract.py`
* Field name normalization → augment extraction normalization pipeline
* Debug filled PDF validator → add diff util (see `NEXT_TASKS.md`)
* Graceful shutdown → enhance signal handling in `app.py` + drain logic in `connection_manager.py`

## Coding Constraints
* Don’t block the event loop with large PDF I/O.
* Keep tool names stable.
* Minimize per-turn chatter—model should either request next field or call a tool.

## Quick Start (Dev)
```
pip install pypdf google-genai==0.3.0 websockets
set GEMINI_API_KEY=YOUR_KEY   # PowerShell: $env:GEMINI_API_KEY="YOUR_KEY"
python app.py
open http://localhost:8000/index.html
```

## See Also
* Backlog / roadmap: `NEXT_TASKS.md`
* Human-facing usage & concepts: `README.md`

**Last Updated:** Sept 15, 2025
**Status:** Stable unified core; focusing on validation, shutdown polish, and field normalization.
