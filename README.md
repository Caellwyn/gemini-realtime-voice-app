# Voice Controlled Form Filler (Dating Profile + PDF AcroForm Mode)

This project provides a browser-based, voice-enabled form filling experience using a Gemini realtime audio dialog model.

You can operate in two modes:

1. Dating Profile Mode (original) – Collects 4 structured profile fields with strict turn-taking and confirmation.
2. PDF Form Mode (new) – Upload an AcroForm PDF (≤5MB) and fill all first-page text fields via voice or manual overrides; then download a still-editable filled PDF.

Code portions inspired by work from [yeyu](https://github.com/yeyu2/Youtube_demos/tree/main/gemini20-realtime-function).

---

## Quick Start

1. Install dependencies (from workspace root or this folder):

   ```powershell
   pip install pypdf google-genai==0.3.0 websockets
   ```

2. Set your Gemini API key (PowerShell example):

   ```powershell
   $env:GEMINI_API_KEY = "YOUR_KEY_HERE"
   ```

3. Run the HTTP static/file + REST server (serves `index.html` and PDF endpoints):

   ```powershell
   python server.py
   ```

4. In a second terminal, start the websocket realtime bridge:

   ```powershell
   python main.py
   ```

5. Open the UI: <http://localhost:8000/index.html>

---

## Modes Overview

### 1. Dating Profile Mode

Collects exactly: `eye_color`, `age`, `ideal_date`, `todays_date`. The system instruction enforces:

- Only the next missing field is proactively requested.
- Multi-field utterances are accepted when user volunteers multiple values.
- Tool calls: `fill_dating_profile` and `get_profile_state`.
- Completion requires explicit user confirmation after all filled.

### 2. PDF Form Mode (New)

Workflow:

1. Choose “Upload AcroForm PDF”.
2. Select PDF (≤5MB). Server extracts first-page AcroForm fields (safety cap, default 300). Duplicate field names get suffixed (`_2`, `_3`).
3. UI renders generic text inputs (all required, initially empty). Fields can be filled:
   - By voice: model calls `fill_form_fields` with user-provided values.
   - Manually: editing an input sends a `user_edit` websocket message.
4. When all fields are filled, the server sends `form_complete:true`; UI shows a confirm prompt.
5. On confirmation (`confirm_form: true`), server returns `download_ready:true` enabling a filled PDF download (still editable; not flattened).
6. Idle sessions (no activity ≥10 min) are purged; UI shows an inactivity banner on next poll.

---

## Architecture

Component | Responsibilities
----------|------------------
`server.py` | Static file hosting + REST endpoints (`/upload_form`, `/download_filled/<id>`, `/reset_form`, `/update_form_state`, `/form_status/<id>`)
`main.py` | WebSocket bridge to Gemini realtime API: tool call mediation, mode branching, audio streaming
`pdf_form/` | Modular PDF logic (`extract.py`, `fill.py`, `schema.py`, `storage.py`, `session_state.py`)
Frontend (`index.html`) | Mode selection, audio capture/playback, dynamic form rendering, websocket client, PDF UI

---

## PDF Extraction Rules

- Only `/AcroForm` fields (no OCR yet).
- Only first page fields kept (`metadata.truncated_to_first_page = True`).
- Field ordering: by widget rectangle (top-to-bottom, then left-to-right) if coordinates exist; otherwise original order.
- Duplicate original names get suffixed `_2`, `_3`, etc. Mapping stored in `metadata.write_name_map` for filling.
- Cap (`MAX_FIELDS=300`) prevents runaway forms; `metadata.field_cap_reached` signals truncation.

---

## REST Endpoints

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
`{ setup: {..., mode: 'dating'\| 'pdf_form' } }` | Initialize session & configure model/tools
`{ realtime_input: {...} }` | Audio/text streaming
`{ user_edit: { field, value } }` | Manual override (dating or PDF mode)
`{ pdf_schema: { ...schema... } }` | Provide parsed schema to model so dynamic tools can be registered
`{ confirm_form: true }` | User confirmed all PDF fields

### WebSocket Messages (Server → Client)

Message | Description
--------|------------
`{ text: "..." }` | Model textual response
`{ audio: base64, audio_mime_type }` | Model audio chunk
`{ profile_tool_response: {...} }` | Dating mode tool update
`{ profile_state_snapshot: {...} }` | Dating state query result
`{ form_tool_response: { updated:{...}, remaining:int } }` | PDF tool update
`{ form_state: {...} }` | PDF state snapshot
`{ form_complete: true }` | All PDF fields filled; prompt for confirm
`{ download_ready: true, form_id }` | Confirmed & filled PDF is ready
`{ error: "message" }` | Error condition (may include `unknown_form`)

---

## Tool Definitions

Dating Mode Tools:

- `fill_dating_profile` (eye_color, age, ideal_date, todays_date)
- `get_profile_state`

PDF Mode Tools (dynamic):

- `fill_form_fields` – Arbitrary subset of schema field names with string values (≤500 chars)
- `get_form_state`

Server validation for `fill_form_fields`:

- Unknown field → ignored (reported to model)
- Empty/whitespace → ignored
- >500 chars → truncated
- Accepts multi-field updates

---

## Completion Logic (PDF Mode)

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
`main.py` | Dynamic tool injection after receiving `pdf_schema` message (Gemini live session update)
`index.html` | Minimal dependency (Material Design Lite + custom styles)

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

Happy building!
