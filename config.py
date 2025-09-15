"""
Configuration settings for the Gemini Real-time Voice Application.
Consolidates all constants and configuration in one place.
"""

# Server Configuration
HTTP_PORT = 8000
WEBSOCKET_PORT = 9082
WEBSOCKET_HOST = "localhost"

# File Upload Limits
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5MB
MAX_PDF_FIELDS = 300  # Safety cap for PDF form fields

# Session Management
FORM_SESSION_TIMEOUT = 600  # 10 minutes
SESSION_CLEANUP_INTERVAL = 180  # 3 minutes
PDF_SYNC_DELAY = 0.3  # Debounce delay for full state sync

# WebSocket Configuration
WEBSOCKET_PING_INTERVAL = 30  # Send keepalive pings every 30 seconds
# Setting timeout to None disables automatic close on missing pong (helpful when model processing may exceed interval)
WEBSOCKET_PING_TIMEOUT = None  # None => treat as 'disabled' in logging
LATENCY_MEASUREMENT_INTERVAL = 30  # Seconds between latency measurements

# Model Configuration
DEFAULT_MODEL = "gemini-2.5-flash-preview-native-audio-dialog"
ALTERNATIVE_MODEL = "gemini-2.0-flash-live-001"

# Audio Configuration
AUDIO_MIME_TYPE = "audio/pcm;rate=16000"
AUDIO_RATE = 16000

# System Instructions
PDF_FORM_INSTRUCTION_TEMPLATE = (
    "You are collecting values for an uploaded PDF form with {total} fields. All are required. "
    "CRITICAL: After EVERY user utterance that provides field values, you MUST call update_pdf_fields immediately to save those values. "
    "Never guess or invent. Ask only for the NEXT missing field in visual order unless the user voluntarily gives multiple in one utterance. "
    "If uncertain about progress call get_form_state. When the user provides ANY field value(s), IMMEDIATELY call update_pdf_fields with an 'updates' parameter containing a JSON string mapping field names to values, e.g. '{{\"FirstName\": \"Alice\", \"LastName\": \"Smith\"}}'. "
    "The updates parameter must be a valid JSON string. Do not restate unchanged fields. ALWAYS call the tool when values are provided, then ask for the next field. After all fields are filled ask for a single confirmation. After user confirms stop. No chit-chat."
)

# Field Value Limits
MAX_FIELD_VALUE_LENGTH = 500
DATE_FORMAT = "YYYY-MM-DD"

# PDF Form Configuration
INTERNAL_FIELD_NAMES = {
    # Use all lowercase for case-insensitive matching
    "formid", "pdf_submission_new", "simple_spc", "adobewarning",
    "submit", "print", "clear", "reset"
}

INTERNAL_FIELD_PATTERNS = [
    "adobewarning",
    "_spc",  # spacer artifacts
]

# Tool Declarations
PDF_TOOL_DECLARATIONS = [
    {
        "name": "update_pdf_fields",
        "description": "Update one or more PDF form fields explicitly provided by the user.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "updates": {"type": "STRING", "description": "JSON string mapping fieldName -> value. Example: '{\"FirstName\": \"Alice\", \"LastName\": \"Smith\"}'"}
            }
        }
    },
    {
        "name": "get_form_state",
        "description": "Retrieve current PDF form progress, counts, and remaining sample. Call if unsure or after unknown_fields.",
        "parameters": {"type": "OBJECT", "properties": {}}
    }
]

# Logging Configuration
LOG_FILE_LATENCY = 'websocket_latency.log'
LOG_FILE_TOOLS = 'tool_calls.log'
LOG_FORMAT = '%(asctime)s - %(levelname)s - %(message)s'

# Error Messages
ERROR_MESSAGES = {
    'bad_content_type': 'Expected multipart/form-data',
    'file_too_large': 'File too large (>5MB)',
    'no_file': 'No file part named file',
    'not_pdf': 'Not a PDF file',
    'encrypted_pdf': 'Encrypted PDF not supported',
    'not_acroform': 'PDF has no AcroForm',
    'no_fields': 'AcroForm present but no fields on first page',
    'parse_failed': 'Failed to parse PDF',
    'unknown_form': 'Unknown form_id',
    'incomplete': 'Form not fully filled',
    'missing_original': 'Original PDF missing',
    'fill_failed': 'Failed to fill PDF form',
    'reset_failed': 'Failed to reset form',
    'internal_error': 'Internal server error'
}

# Voice Configuration
DEFAULT_VOICE = "Puck"
VOICE_OPTIONS = [
    ("Puck", "Conversational, friendly"),
    ("Charon", "Deep, authoritative"),
    ("Kore", "Neutral, professional"),
    ("Fenrir", "Warm, approachable"),
    ("Leda", "Youthful"),
    ("Orus", "Firm"),
    ("Aoede", "Breezy"),
    ("Callirrhoe", "Easy-going"),
    ("Enceladus", "Breathy"),
    ("Iapetus", "Clear"),
    ("Umbriel", "Easy-going"),
    ("Algieba", "Smooth"),
    ("Despina", "Smooth"),
    ("Erinome", "Clear"),
    ("Algenib", "Gravelly"),
    ("Rasalgethi", "Informative"),
    ("Laomedeia", "Upbeat"),
    ("Achernar", "Soft"),
    ("Alnilam", "Firm"),
    ("Schedar", "Even"),
    ("Gacrux", "Mature"),
    ("Pulcherrima", "Forward"),
    ("Achird", "Friendly"),
    ("Zubenelgenubi", "Casual"),
    ("Vindemiatrix", "Gentle"),
    ("Sadachbia", "Lively"),
    ("Sadaltager", "Knowledgeable"),
    ("Sulafat", "Warm"),
    ("Zephyr", "Bright"),
]