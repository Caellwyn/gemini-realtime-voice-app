## pip install google-genai==0.3.0

import asyncio
import json
import os
import websockets
from google import genai
from google.genai import types
import base64
import time
import traceback
import logging
from datetime import datetime

from pdf_form.catalog import compute_field_catalog, build_initial_system_message
from pdf_form.updater import apply_pdf_field_updates
from logging_utils import log_tool_call

# Set up latency logging
latency_logger = logging.getLogger('websocket_latency')
latency_logger.setLevel(logging.INFO)

# Create file handler for latency logs
latency_handler = logging.FileHandler('websocket_latency.log')
latency_handler.setLevel(logging.INFO)

# Create formatter
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
latency_handler.setFormatter(formatter)

# Add handler to logger
latency_logger.addHandler(latency_handler)

# Prevent propagation to root logger to avoid console spam
latency_logger.propagate = False

# Load API key from environment
os.environ['GOOGLE_API_KEY'] = os.getenv('GEMINI_API_KEY')
MODEL = "gemini-2.5-flash-preview-native-audio-dialog"  # use your model ID

# System instructions
PROFILE_SYSTEM_INSTRUCTION = (
    "You collect exactly four profile fields: eye_color (blue|brown|green|hazel), age (1-120 integer), "
    "ideal_date (1-2 concise sentences, no emojis), todays_date (YYYY-MM-DD). "
    "Core rules: NEVER guess or infer any value. Proactive prompts MUST request ONLY the next missing field in this strict order: "
    "eye_color, then age, then ideal_date, then todays_date. "
    "If the user explicitly provides several NEW fields in one utterance (e.g. 'I'm 44 with brown eyes and I like long walks'), you MUST capture all those explicitly stated values together in a single fill_dating_profile tool call containing ONLY those newly provided fields. "
    "Do NOT include unchanged / previously confirmed fields unless they are newly stated in the same utterance. "
    "After the user provides a value (spoken or via a user_edit delta), call fill_dating_profile with ONLY that new field (or fields if multi-field). "
    "If you are unsure what is currently filled, call get_profile_state instead of guessing. "
    "If the user corrects a field, acknowledge the correction briefly and then call fill_dating_profile with ONLY the corrected field. "
    "When all four fields are filled, explicitly ask the user to confirm ALL fields. Do NOT declare completion or stop until the user explicitly confirms. "
    "If after completion the user changes a field, re-confirm ALL fields. "
    "No chit-chat, no small talk, no multiple-field questions at once (unless user volunteered them), no reordering, no extra commentary beyond efficiently collecting and confirming these fields."
)

PDF_FORM_INSTRUCTION_TEMPLATE = (
    "You are collecting values for an uploaded PDF form with {total} fields. All are required. "
    "CRITICAL: After EVERY user utterance that provides field values, you MUST call update_pdf_fields immediately to save those values. "
    "Never guess or invent. Ask only for the NEXT missing field in visual order unless the user voluntarily gives multiple in one utterance. "
    "If uncertain about progress call get_form_state. When the user provides ANY field value(s), IMMEDIATELY call update_pdf_fields with an 'updates' parameter containing a JSON string mapping field names to values, e.g. '{{\"FirstName\": \"Alice\", \"LastName\": \"Smith\"}}'. "
    "The updates parameter must be a valid JSON string. Do not restate unchanged fields. ALWAYS call the tool when values are provided, then ask for the next field. After all fields are filled ask for a single confirmation. After user confirms stop. No chit-chat."
)

client = genai.Client()


# def fill_dating_profile(eye_color: str, age: int, ideal_date: str, todays_date: str):
#     return {
#         "eye_color": eye_color,
#         "age": age,
#         "ideal_date": ideal_date,
#         "todays_date": todays_date,
#     }

dating_tool_declarations = [
    {
        "name": "fill_dating_profile",
        "description": "Record newly provided dating profile field(s). Only include fields the user just explicitly supplied or corrected.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "eye_color": {"type": "STRING", "enum": ["blue","brown","green","hazel"]},
                "age": {"type": "NUMBER"},
                "ideal_date": {"type": "STRING"},
                "todays_date": {"type": "STRING"}
            }
        }
    },
    {
        "name": "get_profile_state",
        "description": "Retrieve current profile state and which fields are still missing.",
        "parameters": {"type": "OBJECT", "properties": {}}
    }
]

pdf_tool_declarations = [
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

async def gemini_session_handler(client_websocket: websockets.ServerProtocol):
    """Handles the interaction with Gemini API within a websocket session.

    Args:
        client_websocket: The websocket connection to the client.
    """
    # Log connection start
    client_addr = f"{client_websocket.remote_address[0]}:{client_websocket.remote_address[1]}"
    latency_logger.info(f"New WebSocket connection from {client_addr}")
    
    # Latency measurement task
    async def measure_latency():
        """Periodically measure and log WebSocket latency"""
        while not client_websocket.closed:
            try:
                start_time = time.time()
                pong_waiter = await client_websocket.ping()
                await pong_waiter
                latency = time.time() - start_time
                latency_ms = latency * 1000
                latency_logger.info(f"Ping latency: {latency_ms:.2f}ms (client: {client_addr})")
                
                # Also log the websocket's built-in latency if available
                if hasattr(client_websocket, 'latency') and client_websocket.latency is not None:
                    built_in_latency_ms = client_websocket.latency * 1000
                    latency_logger.info(f"Built-in latency: {built_in_latency_ms:.2f}ms (client: {client_addr})")
                
                # Wait 30 seconds before next measurement
                await asyncio.sleep(30)
            except websockets.exceptions.ConnectionClosed:
                latency_logger.info(f"Latency measurement stopped - connection closed (client: {client_addr})")
                break
            except Exception as e:
                latency_logger.warning(f"Latency measurement error: {e} (client: {client_addr})")
                await asyncio.sleep(30)  # Continue trying
    
    latency_task = asyncio.create_task(measure_latency())
    
    try:
        config_message = await client_websocket.recv()
        config_data = json.loads(config_message)
        config = config_data.get("setup", {})
        # Allow client to override model per-session
        model_override = config.pop("model", None)
        
        # Flatten generation_config into the main config object for this SDK (expects top-level keys)
        if 'generation_config' in config:
            gen_config = config.pop('generation_config')
            config.update(gen_config)

        # Optional: configure voice via speech_config
        voice_name = config.pop("voice_name", None)
        if voice_name:
            try:
                speech_cfg = types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice_name)
                    ),
                    language_code="en-US",
                )
                config["speech_config"] = speech_cfg
            except Exception as e:
                print(f"Failed to build speech_config for voice '{voice_name}': {e}")

        # Optional: enable server-side VAD (automatic activity detection)
        enable_vad = bool(config.pop("enable_vad", False))
        if enable_vad:
            try:
                aad = types.AutomaticActivityDetection(
                    disabled=False,
                    start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_LOW,
                    end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_LOW,
                    prefix_padding_ms=20,
                    silence_duration_ms=100,
                )
                config["realtime_input_config"] = types.RealtimeInputConfig(
                    automatic_activity_detection=aad
                )
            except Exception as e:
                print(f"Failed to build realtime_input_config: {e}")

        # Extract and remove our internal 'mode' flag so it is NOT sent to Gemini (LiveConnectConfig disallows unknown keys)
        mode = config.pop("mode", "dating")  # 'dating' or 'pdf_form'
        
        # Extract PDF field names if provided (for PDF mode, client should send this in setup)
        pdf_field_names = config.pop("pdf_field_names", [])
        pdf_mode = (mode == 'pdf_form')

        # Dating profile state (default mode)
        profile_state = {"eye_color": None, "age": None, "ideal_date": None, "todays_date": None}
        confirmed_state = {"eye_color": False, "age": False, "ideal_date": False, "todays_date": False}
        session_confirmed = False

        # PDF form state (initialized from field names if provided)
        pdf_state = {name: None for name in pdf_field_names} if pdf_field_names else {}
        pdf_confirmed = {name: False for name in pdf_field_names} if pdf_field_names else {}
        pdf_catalog = compute_field_catalog(pdf_field_names) if pdf_field_names else {"fields": [], "hash": None}
        pdf_form_id = config.pop("pdf_form_id", None)
        pdf_all_confirmed = False

        # Set tools based on mode - must be done before connection
        if pdf_mode and pdf_field_names:
            dynamic_pdf_tool_decls = [
                {
                    "name": "update_pdf_fields",
                    "description": "Update one or more PDF form fields explicitly provided by the user.",
                    "parameters": {
                        "type": "OBJECT",
                        "properties": {
                            'updates': {"type": "STRING", "description": " a flat mapping fieldName -> value. Example: { 'FirstName': 'Alice', 'LastName': 'Smith' }. Be sure to use the field names you were given as the keys, and do NOT invent or guess any fields.",
                        },
                    }}
                },
                {
                    "name": "get_form_state",
                    "description": "Retrieve current PDF form progress, remaining missing fields, and catalog hash.",
                    "parameters": {"type": "OBJECT", "properties": {}}
                }
            ]
            config["tools"] = [{"function_declarations": dynamic_pdf_tool_decls}]
        elif pdf_mode:
            # PDF mode but no fields yet - this shouldn't happen with new flow
            config["tools"] = []
        else:
            config["tools"] = [{"function_declarations": dating_tool_declarations}]

        def missing_fields():
            if pdf_mode:
                return [k for k,v in pdf_state.items() if not v]
            return [k for k,v in profile_state.items() if not v]

        def build_state_snapshot():
            if pdf_mode:
                return {"state": pdf_state, "missing": missing_fields(), "confirmed": pdf_confirmed, "complete": all(pdf_state.values())}
            return {"state": profile_state, "missing": missing_fields(), "confirmed": confirmed_state, "complete": all(profile_state.values())}

        # Log Gemini connection attempt
        latency_logger.info(f"Attempting Gemini API connection (client: {client_addr})")
        gemini_connect_start = time.time()
        
        async with client.aio.live.connect(model=(model_override or MODEL), config=config) as session:
            gemini_connect_time = time.time() - gemini_connect_start
            print("Connected to Gemini API")
            latency_logger.info(f"Gemini API connected in {gemini_connect_time:.2f}s (client: {client_addr})")

            # Send system instruction / priming message
            try:
                if pdf_mode and pdf_field_names:
                    instruction = PDF_FORM_INSTRUCTION_TEMPLATE.format(total=len(pdf_field_names))
                    catalog_msg = build_initial_system_message(pdf_field_names, pdf_catalog["hash"])
                    # Combine into single comprehensive message to ensure it gets through
                    combined_msg = f"{instruction}\n\n{catalog_msg}\n\nCatalog hash: {pdf_catalog['hash']}\nBegin by requesting the value for the first missing field: {pdf_field_names[0]}"
                    await session.send_realtime_input(text=combined_msg)
                    # Small delay to ensure message is processed before audio starts
                    await asyncio.sleep(0.1)
                elif pdf_mode:
                    await session.send_realtime_input(text="PDF form mode active, but no fields available. This shouldn't happen.")
                else:
                    await session.send_realtime_input(text=PROFILE_SYSTEM_INSTRUCTION)
                    await session.send_realtime_input(text="Please provide your eye color (blue, brown, green, or hazel).")
                    await asyncio.sleep(0.1)
            except Exception as e:
                print(f"Failed to send system instruction: {e}")

            session_closed = asyncio.Event()

            async def send_to_gemini():
                """Sends messages from the client websocket to the Gemini API."""
                try:
                    async for message in client_websocket:
                        try:
                            data = json.loads(message)
                            if "realtime_input" in data:
                                ri = data["realtime_input"]
                                # Handle audio chunks (base64 PCM16)
                                for chunk in ri.get("media_chunks", []):
                                    if chunk.get("mime_type") == "audio/pcm" and "data" in chunk:
                                        try:
                                            raw = base64.b64decode(chunk["data"])  # decode before sending to Gemini
                                            if session_closed.is_set():
                                                break
                                            try:
                                                await session.send_realtime_input(
                                                    media=types.Blob(data=raw, mime_type="audio/pcm;rate=16000")
                                                )
                                            except Exception as se:
                                                if isinstance(se, websockets.exceptions.ConnectionClosed):
                                                    if se.code == 1011:
                                                        print("Audio send failed: keepalive timeout")
                                                    else:
                                                        print(f"Audio send failed: connection closed ({se})")
                                                    session_closed.set()
                                                    break
                                                raise
                                        except Exception as de:
                                            print(f"Decode/send audio error: {de}")
                                            traceback.print_exc()
                                # Optional inline text instruction from client
                                text_msg = ri.get("text")
                                if isinstance(text_msg, str) and text_msg.strip():
                                    try:
                                        await session.send_realtime_input(text=text_msg.strip())
                                    except Exception as te:
                                        print(f"Error sending text to Gemini: {te}")
                                # Optional explicit end-of-turn commit from client
                                if ri.get("audio_stream_end") is True:
                                    try:
                                        await session.send_realtime_input(audio_stream_end=True)
                                    except Exception as ce:
                                        print(f"audio_stream_end error: {ce}")
                            elif "user_edit" in data:
                                ue = data["user_edit"]
                                field = ue.get("field")
                                value = ue.get("value")
                                if pdf_mode:
                                    if field in pdf_state:
                                        pdf_state[field] = value[:500]
                                        pdf_confirmed[field] = True
                                        msg = (
                                            f"User explicitly set {field} = {value}. Ask only for the next missing field." if missing_fields() else
                                            "All fields now provided. Ask user for final confirmation."
                                        )
                                        try:
                                            await session.send_realtime_input(text=msg)
                                        except Exception as te:
                                            print(f"Error sending user_edit delta to model: {te}")
                                else:
                                    if field in profile_state:
                                        profile_state[field] = value
                                        confirmed_state[field] = True
                                        session_confirmed = False
                                        msg = (
                                            f"User explicitly set {field} = {value}. Ask only for the next missing field or confirm all if none missing." if missing_fields() else
                                            "User adjusted a field; reconfirm all fields with the user."
                                        )
                                        try:
                                            await session.send_realtime_input(text=msg)
                                        except Exception as te:
                                            print(f"Error sending user_edit delta to model: {te}")
                            elif "confirm_form" in data and pdf_mode:
                                # User confirmed PDF form values; trigger download_ready event
                                if not missing_fields():
                                    try:
                                        await session.send_realtime_input(text="User confirmed all fields. Session will conclude.")
                                    except Exception:
                                        pass
                                    # Notify UI to enable download
                                    try:
                                        await client_websocket.send(json.dumps({"download_ready": True, "form_id": pdf_form_id}))
                                    except Exception as e:
                                        print("Failed to send download_ready", e)
                                    await asyncio.sleep(0.4)
                                    try:
                                        await session.close()
                                    except Exception:
                                        pass
                                    return
                              
                        except Exception as e:
                            print(f"Error sending to Gemini: {e}")
                    print("Client connection closed (send)")
                except websockets.exceptions.ConnectionClosed as e:
                    if e.code == 1011:
                        print("Send connection closed due to keepalive timeout")
                    else:
                        print(f"Send websocket connection closed: {e}")
                except Exception as e:
                    print(f"Error sending to Gemini: {e}")
                finally:
                    print("send_to_gemini closed")
                    session_closed.set()



            async def receive_from_gemini():
                """Receives responses from the Gemini API and forwards them to the client, looping until turn is complete."""
                try:
                    while True:
                        # Reduced logging: only errors and exceptions
                        async for response in session.receive():
                            # Tool call handling (no server_content indicates tool call container in this SDK)
                            if response.server_content is None and response.tool_call is not None:
                                # Reduced logging: only log tool call errors, not every call
                                function_calls = response.tool_call.function_calls
                                function_responses = []
                                for function_call in function_calls:
                                    name = function_call.name
                                    args = function_call.args
                                    call_id = function_call.id
                                    tool_start = time.time()

                                    # PDF mode tools
                                    if pdf_mode and name == "get_form_state":
                                        snap = build_state_snapshot()
                                        # augment with catalog hash & counts
                                        snap_meta = {
                                            "state": snap["state"],
                                            "missing": snap["missing"],
                                            "complete": snap["complete"],
                                            "catalog_hash": pdf_catalog["hash"],
                                            "remaining_count": len(snap["missing"]),
                                            "filled_count": len([k for k,v in snap["state"].items() if v]),
                                            "remaining_sample": snap["missing"][:10],
                                        }
                                        function_responses.append({
                                            "name": name,
                                            "response": {"result": snap_meta},
                                            "id": call_id
                                        })
                                        await client_websocket.send(json.dumps({"form_state": snap_meta}))
                                        log_tool_call(session_id="pdf", tool_name=name, request=args or {}, response=snap_meta, started_ts=tool_start)
                                        continue
                                    if pdf_mode and name == "update_pdf_fields":
                                        # Parse updates from string or dict format
                                        updates_dict = {}
                                        if isinstance(args, dict):
                                            if 'updates' in args:
                                                updates_value = args.get('updates')
                                                if isinstance(updates_value, str):
                                                    # Parse JSON string to dictionary
                                                    try:
                                                        parsed_updates = json.loads(updates_value)
                                                        if isinstance(parsed_updates, dict):
                                                            updates_dict.update({k: v for k, v in parsed_updates.items() if isinstance(k, str)})
                                                        else:
                                                            print(f"Warning: updates string parsed to non-dict: {type(parsed_updates)}")
                                                    except (json.JSONDecodeError, TypeError) as e:
                                                        print(f"Error parsing updates JSON string: {e}")
                                                        print(f"Raw updates value: {repr(updates_value)}")
                                                elif isinstance(updates_value, dict):
                                                    # Direct dict format (backward compatibility)
                                                    updates_dict.update({k: v for k, v in updates_value.items() if isinstance(k, str)})
                                                # Merge any additional top-level dynamic keys (excluding reserved 'updates')
                                                for k, v in args.items():
                                                    if k != 'updates' and isinstance(k, str):
                                                        updates_dict[k] = v
                                            else:
                                                # Fallback: treat entire args as flat mapping
                                                updates_dict.update({k: v for k, v in args.items() if isinstance(k, str)})
                                        else:
                                            updates_dict = {}
                                        summary = apply_pdf_field_updates(updates_dict, pdf_state, pdf_confirmed, list(pdf_state.keys()))
                                        summary["catalog_hash"] = pdf_catalog["hash"]
                                        function_responses.append({
                                            "name": name,
                                            "response": {"result": summary},
                                            "id": call_id
                                        })
                                        if summary.get("applied"):
                                            await client_websocket.send(json.dumps({
                                                "form_tool_response": {
                                                    "updated": summary.get("applied"),
                                                    "remaining": summary.get("remaining_empty_count"),
                                                    "unknown": summary.get("unknown_fields"),
                                                    "catalog_hash": pdf_catalog["hash"]
                                                }
                                            }))
                                        if summary.get("unknown_fields"):
                                            await session.send_realtime_input(text=(
                                                "Unknown field names detected: " + ", ".join(summary["unknown_fields"]) + ". Call get_form_state if unsure."))
                                        if summary.get("complete"):
                                            await client_websocket.send(json.dumps({"form_complete": True}))
                                            await session.send_realtime_input(text="All fields captured. Ask the user to confirm all values are correct.")
                                        # Enhanced logging for debugging
                                        log_tool_call(session_id="pdf", tool_name=name, request={"raw_args": args, "parsed_updates": updates_dict}, response=summary, started_ts=tool_start)
                                        continue
                                    

                                    # Dating mode tools
                                    if not pdf_mode and name == "get_profile_state":
                                        snap = build_state_snapshot()
                                        function_responses.append({
                                            "name": name,
                                            "response": {"result": snap},
                                            "id": call_id
                                        })
                                        await client_websocket.send(json.dumps({"profile_state_snapshot": snap}))
                                        continue
                                    if not pdf_mode and name == "fill_dating_profile":
                                        updated = {}
                                        errors = []
                                        try:
                                            if "eye_color" in args:
                                                ec = str(args.get("eye_color", "")).lower()
                                                if ec in ["blue","brown","green","hazel"]:
                                                    profile_state["eye_color"] = ec; updated["eye_color"] = ec; confirmed_state["eye_color"] = True
                                                else:
                                                    errors.append("eye_color must be one of blue,brown,green,hazel")
                                            if "age" in args:
                                                try:
                                                    age_val = int(args.get("age"))
                                                    if 1 <= age_val <= 120:
                                                        profile_state["age"] = age_val; updated["age"] = age_val; confirmed_state["age"] = True
                                                    else:
                                                        errors.append("age out of range 1-120")
                                                except Exception:
                                                    errors.append("age not an integer")
                                            if "ideal_date" in args:
                                                ideal_date_val = str(args.get("ideal_date", "")).strip()
                                                if ideal_date_val:
                                                    profile_state["ideal_date"] = ideal_date_val; updated["ideal_date"] = ideal_date_val; confirmed_state["ideal_date"] = True
                                                else:
                                                    errors.append("ideal_date empty")
                                            if "todays_date" in args:
                                                td = str(args.get("todays_date", "")).strip()
                                                if len(td)==10 and td.count('-')==2:
                                                    profile_state["todays_date"] = td; updated["todays_date"] = td; confirmed_state["todays_date"] = True
                                                else:
                                                    errors.append("todays_date format must be YYYY-MM-DD")
                                        except Exception as e:
                                            errors.append(f"unexpected error: {e}")

                                        if errors and not updated:
                                            function_responses.append({
                                                "name": name,
                                                "response": {"result": {}, "errors": errors},
                                                "id": call_id
                                            })
                                            await session.send_realtime_input(text="Validation error: " + "; ".join(errors) + ". Please restate only the invalid field.")
                                        else:
                                            function_responses.append({
                                                "name": name,
                                                "response": {"result": updated, "errors": errors},
                                                "id": call_id
                                            })
                                            if updated:
                                                await client_websocket.send(json.dumps({"profile_tool_response": updated}))
                                            if errors:
                                                await session.send_realtime_input(text="Partial success. Still needs fix: " + "; ".join(errors))
                                            if all(profile_state.values()) and not session_confirmed:
                                                await session.send_realtime_input(text="Please confirm all fields are correct. Say something like 'Yes, everything is correct' or specify changes.")
                                            # Reduced logging: removed routine update prints
                                        continue

                                # After accumulating responses, send them back if any
                                if 'function_responses' in locals() and function_responses:
                                    # Reduced logging: removed function_responses dumps
                                    await session.send_tool_response(function_responses=function_responses)
                                    # CRITICAL: Clear the list after sending to prevent context accumulation
                                    function_responses.clear()
                                    continue

                            # Model content
                            if response.server_content is not None:
                                model_turn = response.server_content.model_turn
                                if model_turn:
                                    for part in model_turn.parts:
                                        if hasattr(part, 'text') and part.text is not None:
                                            await client_websocket.send(json.dumps({"text": part.text}))
                                        elif hasattr(part, 'inline_data') and part.inline_data is not None:
                                            # Reduced logging: removed mime_type prints
                                            base64_audio = base64.b64encode(part.inline_data.data).decode('utf-8')
                                            await client_websocket.send(json.dumps({
                                                "audio": base64_audio,
                                                "audio_mime_type": getattr(part.inline_data, 'mime_type', 'audio/pcm;rate=16000')
                                            }))
                                            # Reduced logging: removed audio received prints
                                if response.server_content.turn_complete:
                                    # Reduced logging: removed turn complete prints
                                    pass
                        # End async for
                except websockets.exceptions.ConnectionClosedOK:
                    print("Client connection closed normally (receive)")
                    latency_logger.info(f"Client connection closed normally (client: {client_addr})")
                except websockets.exceptions.ConnectionClosed as e:
                    if e.code == 1011:
                        print("Connection closed due to keepalive timeout - with new settings, this should be much less frequent")
                        latency_logger.warning(f"Connection closed - keepalive timeout (code 1011) (client: {client_addr})")
                    else:
                        print(f"Websocket connection closed: {e}")
                        latency_logger.warning(f"Connection closed unexpectedly: {e} (client: {client_addr})")
                except Exception as e:
                    print(f"Error receiving from Gemini: {e}")
                    latency_logger.error(f"Error in receive loop: {e} (client: {client_addr})")
                finally:
                    print("Gemini connection closed (receive)")
                    session_closed.set()


            # Start send and receive loops
            send_task = asyncio.create_task(send_to_gemini())
            receive_task = asyncio.create_task(receive_from_gemini())
            await asyncio.gather(send_task, receive_task)


    except Exception as e:
        print(f"Error in Gemini session: {e}")
        latency_logger.error(f"Error in Gemini session: {e} (client: {client_addr})")
    finally:
        print("Gemini session closed.")
        latency_logger.info(f"Gemini session ended (client: {client_addr})")
        # Cancel the latency measurement task
        if 'latency_task' in locals():
            latency_task.cancel()


async def main() -> None:
    # Configure websocket keepalive settings to handle long AI processing times
    # ping_interval=30: Send keepalive pings every 30 seconds (instead of default 20)
    # ping_timeout=None: Disable ping timeout to handle variable AI response latency
    # This prevents "keepalive ping timeout" errors during long AI processing
    async with websockets.serve(
        gemini_session_handler, 
        "localhost", 
        9082,
        ping_interval=30,  # Increase ping interval to reduce network traffic
        ping_timeout=None  # Disable timeout to handle AI processing delays
    ):
        print("Running websocket server localhost:9082...")
        await asyncio.Future()  # Keep the server running indefinitely


if __name__ == "__main__":
    asyncio.run(main())