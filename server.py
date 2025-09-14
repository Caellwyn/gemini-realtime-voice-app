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

PORT = 8000
MAX_SIZE = 5 * 1024 * 1024  # 5MB

storage_manager = FormStorageManager()
storage_manager.start_background_cleanup()

# In-memory mapping: form_id -> { schema, state, confirmed }
FORM_SESSIONS = {}

INACTIVITY_TIMEOUT = 600  # 10 minutes

def _cleanup_form_sessions(interval: int = 180):  # every 3 minutes
    import threading
    def loop():
        while True:
            try:
                now = time.time()
                stale = []
                for fid, data in list(FORM_SESSIONS.items()):
                    if now - data.get('last_activity', now) > INACTIVITY_TIMEOUT:
                        stale.append(fid)
                for fid in stale:
                    try:
                        del FORM_SESSIONS[fid]
                        storage_manager.delete(fid)
                        print(f"[cleanup] Removed inactive form session {fid}")
                    except Exception:
                        pass
            except Exception:
                pass
            time.sleep(interval)
    t = threading.Thread(target=loop, daemon=True)
    t.start()

_cleanup_form_sessions()

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
        self.end_headers()
        self.wfile.write(data)

    # ---- Helpers ----
    def _parse_multipart(self):
        content_type = self.headers.get('Content-Type','')
        match = re.match(r'multipart/form-data; *boundary=(.+)', content_type, re.I)
        if not match:
            return None, 'bad_content_type'
        boundary = match.group(1)
        length = int(self.headers.get('Content-Length','0'))
        if length > MAX_SIZE:
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
            if any(FORM_SESSIONS):
                # Auto-clear previous session so user can upload a new PDF without manual reset
                for fid in list(FORM_SESSIONS.keys()):
                    try:
                        del FORM_SESSIONS[fid]
                        storage_manager.delete(fid)
                    except Exception:
                        pass
                replaced_previous = True
            parsed, err = self._parse_multipart()
            if err:
                status = 400 if err != 'internal_error' else 500
                msg_map = {
                    'bad_content_type':'Expected multipart/form-data',
                    'file_too_large':'File too large (>5MB)',
                    'no_file':'No file part named file'
                }
                self._send_json({'ok': False,'error':err,'message':msg_map.get(err, err)}, status); return
            filename, file_bytes = parsed
            if len(file_bytes) > MAX_SIZE:
                self._send_json({'ok': False,'error':'file_too_large','message':'File too large (>5MB)'}, 400); return
            if not file_bytes.startswith(b'%PDF'):
                self._send_json({'ok': False,'error':'not_pdf','message':'Not a PDF file'}, 400); return
            # Encryption check
            try:
                reader = PdfReader(BytesIO(file_bytes))
                if getattr(reader, 'is_encrypted', False):
                    self._send_json({'ok': False,'error':'encrypted_pdf','message':'Encrypted PDF not supported'}, 400); return
            except Exception:
                pass
            try:
                schema = extract_acroform(file_bytes, filename)
            except NotAcroFormError:
                self._send_json({'ok': False,'error':'not_acroform','message':'PDF has no AcroForm'}, 400); return
            except NoAcroFormFieldsError:
                self._send_json({'ok': False,'error':'no_fields','message':'AcroForm present but no fields on first page'}, 400); return
            except Exception as e:
                self._send_json({'ok': False,'error':'parse_failed','message': str(e)}, 500); return

            form_id = schema.form_id
            storage_manager.create(file_bytes, filename, form_id=form_id)
            schema.metadata['write_name_map'] = {f.name: f.original_name for f in schema.fields}
            now = time.time()
            FORM_SESSIONS[form_id] = {
                'schema': schema,
                'state': {fname: None for fname in schema.ordered_field_names()},
                'confirmed': {fname: False for fname in schema.ordered_field_names()},
                'last_activity': now,
                'completed': False,
            }
            warn = []
            if schema.metadata.get('total_fields_raw',0) > len(schema.fields):
                warn.append('fields_truncated')
            if schema.metadata.get('truncated_to_first_page'):
                warn.append('first_page_only')
            payload = {'ok': True, 'schema': schema.to_public_dict(), 'replaced_previous': replaced_previous}
            if warn:
                payload['warnings'] = warn
            self._send_json(payload, 200)
        except Exception as e:
            # Basic logging to stderr / console
            try:
                print(f"[upload_error] {e}")
            except Exception:
                pass
            self._send_json({'ok': False,'error':'internal_error','message': str(e)}, 500)

    def handle_download_filled(self, form_id: str):
        sess = FORM_SESSIONS.get(form_id)
        if not sess:
            self._send_json({'ok': False,'error':'unknown_form','message':'Unknown form_id'}, 404); return
        schema = sess['schema']; state = sess['state']
        if not all(state.values()):
            self._send_json({'ok': False,'error':'incomplete','message':'Form not fully filled'}, 400); return
        original_path = os.path.join(storage_manager.base_dir, form_id, 'original.pdf')
        if not os.path.exists(original_path):
            self._send_json({'ok': False,'error':'missing_original','message':'Original PDF missing'}, 500); return
        with open(original_path,'rb') as f: original_bytes = f.read()
        write_map = schema.metadata.get('write_name_map', {})
        translated_state = {write_map.get(k, k): v for k,v in state.items() if v is not None}
        try:
            filled_bytes = fill_acroform(original_bytes, translated_state)
        except Exception as e:
            self._send_json({'ok': False,'error':'fill_failed','message': str(e)}, 500); return
        self.send_response(200)
        self.send_header('Content-Type', 'application/pdf')
        self.send_header('Content-Disposition', f'attachment; filename="filled_{schema.metadata.get("original_filename","form")}"')
        self.send_header('Content-Length', str(len(filled_bytes)))
        self.end_headers(); self.wfile.write(filled_bytes)

    def handle_reset_form(self):
        try:
            for fid in list(FORM_SESSIONS.keys()):
                del FORM_SESSIONS[fid]
                storage_manager.delete(fid)
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
            if not form_id or form_id not in FORM_SESSIONS:
                self._send_json({'ok': False,'error':'unknown_form'}, 404); return
            sess = FORM_SESSIONS[form_id]
            schema = sess['schema']
            state = sess['state']
            confirmed = sess['confirmed']
            changed = {}
            for k,v in updates.items():
                if k in state and isinstance(v, str) and v.strip():
                    val = v[:500]
                    state[k] = val
                    confirmed[k] = True
                    changed[k] = val
            sess['last_activity'] = time.time()
            complete = all(state.values())
            sess['completed'] = complete
            self._send_json({'ok': True,'updated': changed,'complete': complete,'remaining': len([k for k,v in state.items() if not v])})
        except Exception as e:
            self._send_json({'ok': False,'error':'update_failed','message': str(e)}, 500)

    def handle_form_status(self, form_id: str):
        try:
            sess = FORM_SESSIONS.get(form_id)
            if not sess:
                self._send_json({'ok': False,'error':'unknown_form'}, 404); return
            state = sess['state']
            remaining = [k for k,v in state.items() if not v]
            self._send_json({'ok': True,'remaining': remaining,'complete': len(remaining)==0})
        except Exception as e:
            self._send_json({'ok': False,'error':'status_failed','message': str(e)}, 500)

def run():
    with socketserver.TCPServer(("", PORT), NoCacheHandler) as httpd:
        print("Serving at port", PORT)
        print("Open http://localhost:8000/index.html in your browser.")
        httpd.serve_forever()

if __name__ == "__main__":
    run()
