# Voice Controlled PDF Form Filler (Unified Server)

This project provides a browser-based, voice‑enabled workflow for filling AcroForm PDFs using the Gemini real‑time audio dialog API. The app now runs as a **single process** (`app.py`) hosting both:

* An HTTP server (static UI + REST endpoints for PDF upload, status, download)
* A WebSocket realtime bridge (audio streaming, tool call mediation)

The legacy “dating profile” demo mode has been fully removed to simplify the codebase and reduce latency / complexity. All logic is now **PDF‑only**.

Code portions were originally inspired by work from [yeyu](https://github.com/yeyu2/Youtube_demos/tree/main/gemini20-realtime-function) and have since been heavily refactored.

---

## Quick Start (Unified)

1. Install dependencies:

   ```powershell
   pip install pypdf google-genai==0.3.0 websockets
   ```

2. Export your API key (PowerShell example):

   ```powershell
   $env:GEMINI_API_KEY = "YOUR_KEY_HERE"
   ```

3. Start the unified server (HTTP + WebSocket):

   ```powershell
   python app.py
   ```

4. Open the UI: <http://localhost:8000/index.html>

That’s it—no second process required. If you previously ran `server.py` and `main.py` separately, they are now **deprecated** (still present for reference).

For engineering/backlog details (future improvements, internal priorities), see `NEXT_TASKS.md`. This README stays focused on human usage & concepts.

---

## PDF Form Workflow

Workflow:

1. Upload an AcroForm PDF (≤5MB).
2. First‑page text fields (capped at 300) are extracted, normalized, and displayed.
3. Provide values by speaking; the model MUST call the `update_pdf_fields` tool every time you supply one or more values.
4. You can manually override any field—these edits are immediately synced to the model context and state.
5. When all fields are filled you get a confirmation prompt; confirming enables a filled (still editable) PDF download.
6. Inactive sessions (10 min) are cleaned; the UI will show an inactivity banner if you return.

---

## Architecture

Component | Responsibilities
----------|------------------
`app.py` | Unified entry: spawns HTTP server thread + WebSocket realtime loop
`pdf_form/` | PDF extraction, schema modeling, state & fill operations
`session_manager.py` | In‑memory tracking of active PDF form sessions
`form_manager.py` | Runtime form state (PDF only) and system instruction generation
`tool_response_builder.py` | Standardizes tool responses + client notifications
`audio_handler.py` | Audio chunk encode/decode + playback relay
`websocket_handler.py` | Helper utilities (latency logging, sync helpers, realtime input routing)
`index.html` | Single‑page UI: upload, dynamic field grid, voice assistant toggle

---

## PDF Extraction Rules

- Only `/AcroForm` fields (no OCR yet).
- Only first page fields kept (`metadata.truncated_to_first_page = True`).
- Field ordering: by widget rectangle (top-to-bottom, then left-to-right) if coordinates exist; otherwise original order.
- Duplicate original names get suffixed `_2`, `_3`, etc. Mapping stored in `metadata.write_name_map` for filling.
- Cap (`MAX_FIELDS=300`) prevents runaway forms; `metadata.field_cap_reached` signals truncation.

---

## REST Endpoints (served by `app.py`)

Endpoint | Method | Description | Success Payload
---------|--------|-------------|----------------
`/upload_form` | POST (multipart) | Upload PDF; parse schema | `{ ok, schema, warnings? }`
`/reset_form` | POST | Clears active form session | `{ ok: true }`
`/download_filled/<form_id>` | GET | Download filled PDF (after confirmation) | PDF bytes
`/update_form_state` | POST JSON | (Optional) Batch update subset of fields | `{ ok, updated, complete, remaining }`
`/form_status/<form_id>` | GET | Poll session status | `{ ok, remaining:[...], complete:bool }`

Error payloads include `{ ok:false, error, message? }`.

---

## WebSocket Messages (Client → Server)

Message | Purpose (Mode)
--------|----------------
`{ setup: { generation_config..., voice_name, enable_vad, pdf_field_names[], pdf_form_id } }` | Initialize session & tools
`{ realtime_input: {...} }` | Audio stream chunks and optional inline text
`{ user_edit: { field, value } }` | Manual override
`{ confirm_form: true }` | User confirmed all fields

### WebSocket Messages (Server → Client)

Message | Description
--------|------------
`{ text: "..." }` | Model textual response
`{ audio: base64, audio_mime_type }` | Model audio chunk
`{ form_tool_response: { updated:{...}, remaining:int } }` | Applied field updates
`{ form_state: {...} }` | Snapshot (on explicit model query)
`{ form_complete: true }` | All fields captured, UI should ask user to confirm
`{ download_ready: true, form_id }` | Confirmation accepted; PDF ready to download
`{ error: "message" }` | Error condition (e.g. `unknown_form` after expiration)

---

## Tool Definitions (PDF Only)

Tool | Purpose
-----|--------
`update_pdf_fields` | Persist one or more field values (JSON string in `updates` argument)
`get_form_state` | Retrieve counts + remaining sample when the model is uncertain

Validation rules:
* Unknown field names are ignored (reported back in tool response)
* Empty / whitespace‑only values ignored
* Values > 500 chars truncated
* Multi‑field batches encouraged when user supplies them together

---

## Completion Logic

1. Every successful `fill_form_fields` or `user_edit` updates field value + marks confirmed.
2. When all fields non-null: server sends `{ form_complete:true }` and instructs model to ask for confirmation.
3. Client sends `{ confirm_form:true }` → server responds with `{ download_ready:true, form_id }` and closes session after a short delay.
4. User downloads filled PDF (fields remain editable; not flattened).

---

## Inactivity & Cleanup

- Sessions expire after 10 minutes of inactivity (audio, tool call, edit) via background cleaners (`storage.py` + in-memory session pruner).
- Frontend polls `/form_status/<form_id>` every 30s; on 404 shows a banner: *Session expired due to inactivity*.
- Manual reset: POST `/reset_form` or UI Reset button.

---

## Development Notes

Folder | Notes
-------|------
`pdf_form/` | Modular and testable: can later add OCR, flattening
`app.py` | Unified runner; removes need for multi‑process orchestration
`index.html` | PDF‑only UI with a single Voice Assistant toggle

---

## Light QA Checklist

Scenario | Expected
---------|---------
Upload valid single-page AcroForm | Schema with field_count >0
Upload multipage form | Warning shows (first page only)
Upload PDF no AcroForm | Error message “PDF has no AcroForm”
Upload AcroForm with zero fields | Error “AcroForm present but no fields on first page”
Fill fields via voice | Tool responses appear; field badges update
Manual override after AI fill | Manual value persists; state updated immediately
All fields filled | `form_complete` prompt appears; confirm triggers `download_ready`
Download PDF | Opens with updated editable fields
Reset mid-session | Form cleared; can upload again
Idle >10 min | Session cleaned; inactivity banner on next poll
Oversized value (>500 chars) | Truncated + alert
Unknown field in tool call | Ignored; model receives note (partial/ignored feedback)

---

## Future Enhancements (Not Implemented)

- Checkbox / radio / combo box handling
- Flattened + overlay output
- Multi-page interactive navigation
- Nearest text label inference
- Field type classification & regex enforcement
- Session persistence / database storage
- Better field name parsing & normalization (improve label inference / canonicalization)
- Debug filled PDF download tool (server-side validator / diff of filled vs expected)
- More graceful unified app shutdown (drain active WS sessions, flush logs, structured exit codes)

---

## Next Session Starter Tasks

These are the top three actionable items to tackle first when development resumes:

1. Debug filled PDF download: add integrity/diff utility comparing in-memory state vs. generated PDF (log mismatches).
2. Graceful shutdown improvements: drain active WebSocket sessions, await in-flight tool responses, structured exit code + summary log.
3. Field name normalization pass: strip noise (underscores, trailing numerics), infer labels from nearby text objects, add canonical mapping for prompt clarity.

Keep these small and incremental—each can be delivered independently.

---

## Troubleshooting

Issue | Suggestion
------|-----------
Audio not playing | Ensure browser allowed microphone & autoplay; check console for worklet load errors
No fields after upload | Confirm PDF actually has AcroForm fields (try opening in a PDF editor)
Session expires quickly | Verify system clock; adjust inactivity timeout in `storage.py` / `session_state.py`
Download missing values | Ensure confirmation was sent; re-check if `download_ready` fired in console

---

## License / Attribution

See source headers; example scaffold draws inspiration from linked demos. Adapt as needed.

---

## Migration Notes (from dual‑process architecture)

Old | New
----|----
Run `python server.py` + `python main.py` | Run `python app.py`
Mode negotiation (`mode: dating | pdf_form`) | Removed (always PDF)
Inter‑process pdf_sync HTTP POST | Direct in‑process sync (HTTP fallback retained for legacy multi‑process use)

If you still have local scripts referencing `main.py`, update them to call `app.py`.

---

Happy building!
