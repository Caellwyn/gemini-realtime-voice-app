## pip install google-genai==0.3.0

import asyncio
import json
import os
import websockets
from google import genai
from google.genai import types
import base64

# Load API key from environment
os.environ['GOOGLE_API_KEY'] = os.getenv('GEMINI_API_KEY')
MODEL = "gemini-2.5-flash-preview-native-audio-dialog"  # use your model ID

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

client = genai.Client()


def fill_dating_profile(eye_color: str, age: int, ideal_date: str, todays_date: str):
    return {
        "eye_color": eye_color,
        "age": age,
        "ideal_date": ideal_date,
        "todays_date": todays_date,
    }

tool_schemas = {
    "function_declarations": [
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
}

async def gemini_session_handler(client_websocket: websockets.ServerProtocol):
    """Handles the interaction with Gemini API within a websocket session.

    Args:
        client_websocket: The websocket connection to the client.
    """
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

        config["tools"] = [tool_schemas]

        # Per-session profile state (server-side mirror)
        profile_state = {"eye_color": None, "age": None, "ideal_date": None, "todays_date": None}
        confirmed_state = {"eye_color": False, "age": False, "ideal_date": False, "todays_date": False}
        session_confirmed = False

        def missing_fields():
            return [k for k,v in profile_state.items() if not v]

        def build_state_snapshot():
            return {"state": profile_state, "missing": missing_fields(), "confirmed": confirmed_state, "complete": all(profile_state.values())}

        async with client.aio.live.connect(model=(model_override or MODEL), config=config) as session:
            print("Connected to Gemini API")

            # Send system instruction / priming message
            try:
                await session.send_realtime_input(text=PROFILE_SYSTEM_INSTRUCTION)
                await session.send_realtime_input(text="Please provide your eye color (blue, brown, green, or hazel).")
            except Exception as e:
                print(f"Failed to send system instruction: {e}")

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
                                          await session.send_realtime_input(
                                              media=types.Blob(data=raw, mime_type="audio/pcm;rate=16000")
                                          )
                                      except Exception as de:
                                          print(f"Decode/send audio error: {de}")

                              # Optional inline text instruction from client
                              text_msg = ri.get("text")
                              if isinstance(text_msg, str) and text_msg.strip():
                                  try:
                                      await session.send_realtime_input(text=text_msg.strip())
                                      print("Forwarded client text prompt to Gemini")
                                  except Exception as te:
                                      print(f"Error sending text to Gemini: {te}")

                              # Optional explicit end-of-turn commit from client
                              if ri.get("audio_stream_end") is True:
                                  try:
                                      await session.send_realtime_input(audio_stream_end=True)
                                      print("Forwarded audio_stream_end to Gemini")
                                  except Exception as ce:
                                      print(f"audio_stream_end error: {ce}")
                          elif "user_edit" in data:
                              # Manual override from client; update state & notify model concisely
                              ue = data["user_edit"]
                              field = ue.get("field")
                              value = ue.get("value")
                              if field in profile_state:
                                  profile_state[field] = value
                                  confirmed_state[field] = True  # manual edits are considered confirmed
                                  session_confirmed = False  # any edit after confirmation resets session level confirmation
                                  msg = f"User explicitly set {field} = {value}. Ask only for the next missing field or confirm all if none missing." if missing_fields() else "User adjusted a field; reconfirm all fields with the user."
                                  try:
                                      await session.send_realtime_input(text=msg)
                                  except Exception as te:
                                      print(f"Error sending user_edit delta to model: {te}")
                              
                      except Exception as e:
                          print(f"Error sending to Gemini: {e}")
                  print("Client connection closed (send)")
                except Exception as e:
                     print(f"Error sending to Gemini: {e}")
                finally:
                   print("send_to_gemini closed")



            async def receive_from_gemini():
                """Receives responses from the Gemini API and forwards them to the client, looping until turn is complete."""
                try:
                    while True:
                        try:
                            print("receiving from gemini")
                            async for response in session.receive():
                                #first_response = True
                                #print(f"response: {response}")
                                if response.server_content is None:
                                    if response.tool_call is not None:
                                          #handle the tool call
                                           print(f"Tool call received: {response.tool_call}")

                                           function_calls = response.tool_call.function_calls
                                           function_responses = []

                                           for function_call in function_calls:
                                                name = function_call.name
                                                args = function_call.args
                                                call_id = function_call.id

                                                if name == "get_profile_state":
                                                    snap = build_state_snapshot()
                                                    function_responses.append({
                                                        "name": name,
                                                        "response": {"result": snap},
                                                        "id": call_id
                                                    })
                                                    await client_websocket.send(json.dumps({"profile_state_snapshot": snap}))
                                                elif name == "fill_dating_profile":
                                                    # Robust validation with graceful error return
                                                    updated = {}
                                                    errors = []
                                                    try:
                                                        # eye_color
                                                        if "eye_color" in args:
                                                            ec = str(args.get("eye_color", "")).lower()
                                                            if ec in ["blue","brown","green","hazel"]:
                                                                profile_state["eye_color"] = ec; updated["eye_color"] = ec; confirmed_state["eye_color"] = True
                                                            else:
                                                                errors.append("eye_color must be one of blue,brown,green,hazel")
                                                        # age
                                                        if "age" in args:
                                                            try:
                                                                age_val = int(args.get("age"))
                                                                if 1 <= age_val <= 120:
                                                                    profile_state["age"] = age_val; updated["age"] = age_val; confirmed_state["age"] = True
                                                                else:
                                                                    errors.append("age out of range 1-120")
                                                            except Exception:
                                                                errors.append("age not an integer")
                                                        # ideal_date
                                                        if "ideal_date" in args:
                                                            ideal_date_val = str(args.get("ideal_date", "")).strip()
                                                            if ideal_date_val:
                                                                profile_state["ideal_date"] = ideal_date_val; updated["ideal_date"] = ideal_date_val; confirmed_state["ideal_date"] = True
                                                            else:
                                                                errors.append("ideal_date empty")
                                                        # todays_date
                                                        if "todays_date" in args:
                                                            td = str(args.get("todays_date", "")).strip()
                                                            if len(td)==10 and td.count('-')==2:
                                                                profile_state["todays_date"] = td; updated["todays_date"] = td; confirmed_state["todays_date"] = True
                                                            else:
                                                                errors.append("todays_date format must be YYYY-MM-DD")
                                                    except Exception as e:
                                                        errors.append(f"unexpected error: {e}")

                                                    if errors and not updated:
                                                        # Return an empty result with error messages so model can correct
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
                                                        # If all fields now filled prompt for user confirmation
                                                        if all(profile_state.values()) and not session_confirmed:
                                                            await session.send_realtime_input(text="Please confirm all fields are correct. Say something like 'Yes, everything is correct' or specify changes.")
                                                        print("Dating profile partial update executed", updated, "errors", errors)
                                                    continue


                                           # Send function response back to Gemini
                                           print(f"function_responses: {function_responses}")
                                           await session.send_tool_response(function_responses=function_responses)
                                           continue

                                    #print(f'Unhandled server message! - {response}')
                                    #continue

                                # Avoid accessing response.text when there are non-text parts to prevent SDK warnings

                                model_turn = response.server_content.model_turn
                                if model_turn:
                                    for part in model_turn.parts:
                                        #print(f"part: {part}")
                                        if hasattr(part, 'text') and part.text is not None:
                                            #print(f"text: {part.text}")
                                            await client_websocket.send(json.dumps({"text": part.text}))
                                        elif hasattr(part, 'inline_data') and part.inline_data is not None:
                                            # if first_response:
                                            print("inline_data mime_type:", getattr(part.inline_data, 'mime_type', 'unknown'))
                                                #first_response = False
                                            base64_audio = base64.b64encode(part.inline_data.data).decode('utf-8')
                                            await client_websocket.send(json.dumps({
                                                "audio": base64_audio,
                                                "audio_mime_type": getattr(part.inline_data, 'mime_type', 'audio/pcm;rate=16000')
                                            }))
                                            print("audio received")

                                if response.server_content.turn_complete:
                                    print('\n<Turn complete>')
                        except websockets.exceptions.ConnectionClosedOK:
                            print("Client connection closed normally (receive)")
                            break  # Exit the loop if the connection is closed
                        except Exception as e:
                            print(f"Error receiving from Gemini: {e}")
                            break # exit the lo

                except Exception as e:
                      print(f"Error receiving from Gemini: {e}")
                finally:
                      print("Gemini connection closed (receive)")


            # Start send loop
            send_task = asyncio.create_task(send_to_gemini())
            # Launch receive loop as a background task
            receive_task = asyncio.create_task(receive_from_gemini())
            await asyncio.gather(send_task, receive_task)


    except Exception as e:
        print(f"Error in Gemini session: {e}")
    finally:
        print("Gemini session closed.")


async def main() -> None:
    async with websockets.serve(gemini_session_handler, "localhost", 9082):
        print("Running websocket server localhost:9082...")
        await asyncio.Future()  # Keep the server running indefinitely


if __name__ == "__main__":
    asyncio.run(main())