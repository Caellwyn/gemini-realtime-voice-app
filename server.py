import http.server
import socketserver
import re
import json
import os
import time
from io import BytesIO
from urllib.parse import urlparse
from pdf_form import (
    extract_acroform,
    NoAcroFormFieldsError,
    NotAcroFormError,
    FormStorageManager,
    fill_acroform,
)
from pypdf import PdfReader
from config import (
    HTTP_PORT, MAX_FILE_SIZE, FORM_SESSION_TIMEOUT, 
    SESSION_CLEANUP_INTERVAL, ERROR_MESSAGES
)
from session_manager import get_session_manager
from pdf_extractor import PDFExtractor
from config import ENABLE_LLM_FIELD_NORMALIZATION
from pdf_form.llm_normalizer import normalize_fields
from form_manager import extract_pdf_form_metadata_from_bytes

storage_manager = FormStorageManager()
storage_manager.start_background_cleanup()

# Get the global session manager
session_manager = get_session_manager(storage_manager)

class NoCacheHandler(http.server.SimpleHTTPRequestHandler):
    """HTTP handler serving static files plus form endpoints.

    Endpoints:
      POST /upload_form  (multipart, field name 'file')
      POST /reset_form
      GET  /download_filled/<form_id>
    """

    def end_headers(self):
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        super().end_headers()

    def _send_json(self, obj, status=200):
        data = json.dumps(obj).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        try:
            self.end_headers()
            try:
                self.wfile.write(data)
            except ConnectionAbortedError:
                # Client went away mid-response; ignore silently
                pass
            except OSError:
                pass
        except Exception:
            # Suppress secondary errors attempting to send error responses
            pass

    # ---- Helpers ----
    def _parse_multipart(self):
        content_type = self.headers.get('Content-Type','')
        match = re.match(r'multipart/form-data; *boundary=(.+)', content_type, re.I)
        if not match:
            return None, 'bad_content_type'
        boundary = match.group(1)
        length = int(self.headers.get('Content-Length','0'))
        if length > MAX_FILE_SIZE:
            return None, 'file_too_large'
        body = self.rfile.read(length)
        parts = body.split(('--'+boundary).encode('utf-8'))
        file_bytes = None
        filename = 'uploaded.pdf'
        for part in parts:
            if not part or part in (b'--\r\n', b'--'):
                continue
            header, _, content = part.partition(b'\r\n\r\n')
            if b'Content-Disposition' in header and b'name="file"' in header:
                fn_match = re.search(br'filename="([^"]+)"', header)
                if fn_match:
                    filename = fn_match.group(1).decode('utf-8', 'ignore')
                if content.endswith(b'\r\n'):
                    content = content[:-2]
                file_bytes = content
                break
        if file_bytes is None:
            return None, 'no_file'
        return (filename, file_bytes), None

    # ---- Endpoint handlers ----
    def handle_upload_form(self):
        try:
            replaced_previous = False
            if session_manager.get_session_count() > 0:
                # Auto-clear previous sessions so user can upload a new PDF without manual reset
                session_manager.clear_all_sessions()
                replaced_previous = True
            parsed, err = self._parse_multipart()
            if err:
                status = 400 if err != 'internal_error' else 500
                self._send_json({
                    'ok': False,
                    'error': err,
                    'message': ERROR_MESSAGES.get(err, err)
                }, status)
                return
            filename, file_bytes = parsed
            
            # Use the new PDF extractor for processing
            success, response = PDFExtractor.process_uploaded_pdf(file_bytes, filename)
            if not success:
                status = 400 if response.get('error') != 'internal_error' else 500
                self._send_json(response, status)
                return
            
            # Get form_id from the response (don't re-extract to avoid generating new UUID)
            schema_dict = response['schema']
            form_id = schema_dict['form_id']
            print(f"[upload] Using form_id from response: {form_id}")
            
            # We still need a schema object for session creation, but we'll override its form_id
            result = PDFExtractor.extract_form_schema(file_bytes, filename)
            schema = result.schema
            schema.form_id = form_id  # Use the same form_id as in the response
            print(f"[upload] Set schema.form_id to match response: {schema.form_id}")
            
            storage_manager.create(file_bytes, filename, form_id=form_id)
            schema.metadata['write_name_map'] = {f.name: f.original_name for f in schema.fields}
            # Expose reverse name map for convenience (original->schema) when duplicates were disambiguated
            try:
                reverse_map = {}
                for k, v in schema.metadata['write_name_map'].items():
                    # Only first mapping for an original name kept (representative)
                    reverse_map.setdefault(v, k)
                schema.metadata['original_to_schema'] = reverse_map
            except Exception:
                pass

            # Optional: LLM-based normalization for display names, prompts, and groups
            try:
                if ENABLE_LLM_FIELD_NORMALIZATION:
                    raw_fields = extract_pdf_form_metadata_from_bytes(file_bytes)
                    norm = normalize_fields(file_bytes, raw_fields)
                    by_index = norm.get("by_index", {})
                    groups = norm.get("groups", [])

                    # Apply display names in visual/index order (schema is already ordered)
                    for idx, field in enumerate(schema.fields):
                        n = by_index.get(idx)
                        if not n:
                            continue
                        dn = (n.get("display_name") or "").strip()
                        if dn:
                            field.display_name = dn[:80]

                    # Store metadata for UI/agent consumption
                    schema.metadata.setdefault("llm_normalized", True)
                    # Spoken prompts per index
                    prompts = {}
                    for idx, n in by_index.items():
                        sp = (n.get("spoken_prompt") or "").strip()
                        if sp:
                            prompts[str(idx)] = sp[:140]
                    if prompts:
                        schema.metadata["spoken_prompts"] = prompts
                    if groups:
                        schema.metadata["groups"] = groups
            except Exception as _e:
                # Non-fatal if normalizer fails
                pass

            # Build a unique display alias map -> canonical schema name ALWAYS (even without LLM)
            try:
                alias_to_canonical = {}
                display_counts = {}
                for field in schema.fields:
                    disp = (field.display_name or field.name).strip() or field.name
                    base = disp
                    if base in display_counts:
                        display_counts[base] += 1
                        disp = f"{base} #{display_counts[base]}"
                    else:
                        display_counts[base] = 1
                    alias_to_canonical[disp] = field.name
                schema.metadata["display_alias_to_canonical"] = alias_to_canonical
            except Exception:
                pass
            
            # Create session using session manager
            session = session_manager.create_session(form_id, schema)
            print(f"[upload] Created session with form_id: {form_id}")
            print(f"[upload] Session count after creation: {len(session_manager._sessions)}")
            
            # Add form_id explicitly to response for debugging
            response['form_id'] = form_id
            print(f"[upload] Returning form_id in response: {form_id}")
            print(f"[upload] Schema form_id: {response['schema']['form_id']}")
            print(f"[upload] Full response keys: {list(response.keys())}")
            print(f"[upload] About to call schema.to_public_dict()")
            public_dict = schema.to_public_dict()
            print(f"[upload] public_dict form_id: {public_dict['form_id']}")
            print(f"[upload] Are they equal? {form_id == public_dict['form_id']}")
            
            # Replace response schema with the updated public dict (includes display names & metadata)
            try:
                response['schema'] = schema.to_public_dict()
            except Exception:
                # Fallback: at least update known metadata fields if replacement fails
                try:
                    response['schema']['metadata']['write_name_map'] = schema.metadata.get('write_name_map', {})
                    response['schema']['metadata']['original_to_schema'] = schema.metadata.get('original_to_schema', {})
                except Exception:
                    pass

            # Add replacement info to response
            response['replaced_previous'] = replaced_previous
            # Ensure response schema reflects any updated display names and metadata
            try:
                response['schema'] = schema.to_public_dict()
            except Exception:
                pass
            
            self._send_json(response, 200)
        except Exception as e:
            # Basic logging to stderr / console
            try:
                print(f"[upload_error] {e}")
            except Exception:
                pass
            self._send_json({'ok': False,'error':'internal_error','message': str(e)}, 500)

    def handle_download_filled(self, form_id: str):
        print(f"[download] Attempting download for form_id: {form_id}")
        session = session_manager.get_session(form_id)
        if not session:
            print(f"[download] unknown form_id {form_id} - session not found in session manager")
            # Debug: list all current sessions
            try:
                session_count = session_manager.get_session_count()
                print(f"[download] Current session count: {session_count}")
            except Exception as e:
                print(f"[download] Error getting session count: {e}")
            self._send_json({'ok': False,'error':'unknown_form','message':'Unknown form_id'}, 404); return
        
        print(f"[download] Session found for {form_id}")
        schema = session.schema
        state = session.state
        
        # Allow download if form is complete OR user has confirmed
        is_complete = all(state.values())
        is_confirmed = getattr(session, 'download_confirmed', False)
        
        print(f"[download] Form complete: {is_complete}, Download confirmed: {is_confirmed}")
        
        if not is_complete and not is_confirmed:
            try:
                missing = [k for k,v in state.items() if not v]
                print(f"[download] incomplete and unconfirmed form {form_id}, missing={missing}")
            except Exception: pass
            self._send_json({'ok': False,'error':'incomplete','message':'Form not fully filled and not confirmed'}, 400); return
        original_path = os.path.join(storage_manager.base_dir, form_id, 'original.pdf')
        if not os.path.exists(original_path):
            try: print(f"[download] original missing for {form_id} expected {original_path}")
            except Exception: pass
            self._send_json({'ok': False,'error':'missing_original','message':'Original PDF missing'}, 500); return
        with open(original_path,'rb') as f: original_bytes = f.read()
        print(f"[download] Original PDF size: {len(original_bytes)} bytes")
        write_map = schema.metadata.get('write_name_map', {})
        translated_state = {write_map.get(k, k): v for k,v in state.items() if v is not None}
        print(f"[download] State data: {len(state)} fields, {len(translated_state)} non-null")
        print(f"[download] Sample state: {dict(list(translated_state.items())[:3])}")
        try:
            filled_bytes = fill_acroform(original_bytes, translated_state)
            print(f"[download] Fill successful, filled PDF size: {len(filled_bytes)} bytes")
        except Exception as e:
            try: print(f"[download] fill_acroform failed {e}")
            except Exception: pass
            self._send_json({'ok': False,'error':'fill_failed','message': str(e)}, 500); return
        self.send_response(200)
        self.send_header('Content-Type', 'application/pdf')
        self.send_header('Content-Disposition', f'attachment; filename="filled_{schema.metadata.get("original_filename","form")}"')
        self.send_header('Content-Length', str(len(filled_bytes)))
        print(f"[download] Sending PDF with {len(filled_bytes)} bytes")
        self.end_headers(); self.wfile.write(filled_bytes)

    def handle_reset_form(self):
        try:
            session_manager.clear_all_sessions()
            self._send_json({'ok': True})
        except Exception as e:
            self._send_json({'ok': False,'error':'reset_failed','message': str(e)}, 500)

    # ---- Dispatchers ----
    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == '/upload_form':
            return self.handle_upload_form()
        if parsed.path == '/reset_form':
            return self.handle_reset_form()
        if parsed.path == '/update_form_state':
            return self.handle_update_form_state()
        self.send_error(404, 'Unknown POST endpoint')

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith('/download_filled/'):
            form_id = parsed.path.rsplit('/',1)[-1]
            return self.handle_download_filled(form_id)
        if parsed.path.startswith('/original_pdf/'):
            form_id = parsed.path.rsplit('/',1)[-1]
            return self.handle_original_pdf(form_id)
        if parsed.path.startswith('/form_status/'):
            form_id = parsed.path.rsplit('/',1)[-1]
            return self.handle_form_status(form_id)
        return super().do_GET()

    def handle_update_form_state(self):
        try:
            length = int(self.headers.get('Content-Length','0'))
            raw = self.rfile.read(length) if length else b''
            data = json.loads(raw.decode('utf-8') or '{}')
            form_id = data.get('form_id')
            updates = data.get('updates', {})
            
            print(f"[update] Looking for session with form_id: {form_id}")
            print(f"[update] Current session count: {len(session_manager._sessions)}")
            print(f"[update] Available form_ids: {list(session_manager._sessions.keys())}")
            
            # Correct membership check: use get_session instead of relying on __contains__ (thread-safe path)
            if not form_id or session_manager.get_session(form_id) is None:
                self._send_json({'ok': False,'error':'unknown_form'}, 404); return
            
            # Update session state using session manager
            changed = session_manager.update_session_state(form_id, updates)
            if changed is None:
                self._send_json({'ok': False,'error':'unknown_form'}, 404); return
            
            session = session_manager.get_session(form_id)
            complete = session.is_complete() if session else False
            remaining_count = len(session.get_missing_fields()) if session else 0
            
            try:
                print(f"[update_form_state] form_id={form_id} applied={list(changed.keys())} complete={complete}")
            except Exception:
                pass
            self._send_json({'ok': True,'updated': changed,'complete': complete,'remaining': remaining_count})
        except Exception as e:
            self._send_json({'ok': False,'error':'update_failed','message': str(e)}, 500)

    def handle_form_status(self, form_id: str):
        try:
            status = session_manager.get_session_status(form_id)
            if not status:
                try:
                    print(f"[form_status] unknown form_id {form_id}")
                except Exception:
                    pass
                self._send_json({'ok': False,'error':'unknown_form'}, 404); return
            self._send_json({'ok': True,'remaining': status['remaining'],'complete': status['complete']})
        except Exception as e:
            self._send_json({'ok': False,'error':'status_failed','message': str(e)}, 500)

    def handle_original_pdf(self, form_id: str):
        try:
            session = session_manager.get_session(form_id)
            if not session:
                self.send_error(404, 'Unknown form_id'); return
            original_path = os.path.join(storage_manager.base_dir, form_id, 'original.pdf')
            if not os.path.exists(original_path):
                self.send_error(404, 'Original PDF missing'); return
            with open(original_path, 'rb') as f:
                data = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'application/pdf')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except Exception:
                pass
        except Exception:
            self.send_error(500, 'Failed to serve original PDF')

def run():
    with socketserver.TCPServer(("", HTTP_PORT), NoCacheHandler) as httpd:
        print("Serving at port", HTTP_PORT)
        print(f"Open http://localhost:{HTTP_PORT}/index.html in your browser.")
        httpd.serve_forever()

if __name__ == "__main__":
    run()
