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
    "You are a focused form-filling assistant whose ONLY goal is to collect four fields for a dating profile via the tool 'fill_dating_profile'. "
    "NEVER guess or fabricate values. ALWAYS ask the user directly for each missing field in this strict order: eye_color, age, ideal_date, todays_date. "
    "After the user explicitly provides or confirms a value, immediately call the tool with ONLY the fields you have gathered so far (and any previously confirmed ones) until all four are present. "
    "Do not ask general questions like 'How can I help?'—immediately request the next missing field. "
    "Validation rules:\n"
    "eye_color: one of ['blue','brown','green','hazel'] (lowercase exact).\n"
    "age: integer 1-120 (no decimals).\n"
    "ideal_date: 1-2 concise sentences, no emojis.\n"
    "todays_date: ISO 8601 (YYYY-MM-DD).\n"
    "If user changes a previously provided field, re-call tool with corrected value. Do NOT call tool before the user answers your question. Do NOT invent or infer values."
)

client = genai.Client(
    http_options={
        'api_version': 'v1alpha',
    }
)


def fill_dating_profile(eye_color: str, age: int, ideal_date: str, todays_date: str):
    return {
        "eye_color": eye_color,
        "age": age,
        "ideal_date": ideal_date,
        "todays_date": todays_date,
    }

tool_fill_dating_profile = {
    "function_declarations": [
        {
            "name": "fill_dating_profile",
            "description": "Fill or update a dating profile fields. The model should choose appropriate realistic values. If a value is already provided by prior context, it can still be resent for completeness.",
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "eye_color": {
                        "type": "STRING",
                        "enum": ["blue", "brown", "green", "hazel"],
                        "description": "Eye color; must be exactly one of: blue, brown, green, hazel."
                    },
                    "age": {
                        "type": "NUMBER",
                        "description": "Age as a positive integer (years)."
                    },
                    "ideal_date": {
                        "type": "STRING",
                        "description": "A concise description (1-2 sentences) of the person's ideal date."
                    },
                    "todays_date": {
                        "type": "STRING",
                        "description": "Today's date in ISO 8601 format (YYYY-MM-DD)."
                    }
                },
                "required": ["eye_color", "age", "ideal_date", "todays_date"]
            }
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

        config["tools"] = [tool_fill_dating_profile]

        # Per-session profile state (server-side mirror)
        profile_state = {"eye_color": None, "age": None, "ideal_date": None, "todays_date": None}

        async with client.aio.live.connect(model=(model_override or MODEL), config=config) as session:
            print("Connected to Gemini API")

            # Send system instruction / priming message
            try:
                await session.send_realtime_input(text=PROFILE_SYSTEM_INSTRUCTION)
                # Prompt for first missing field (eye_color) without guessing.
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
                              if ri.get("turn") == "commit":
                                  try:
                                      # Indicate that the audio stream has ended so the model can respond
                                      await session.send_realtime_input(audio_stream_end=True)
                                      print("Forwarded explicit audio_stream_end to Gemini")
                                  except Exception as ce:
                                      print(f"Commit error: {ce}")
                              
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

                                                if name == "fill_dating_profile":
                                                    try:
                                                        eye_color = str(args.get("eye_color", "")).lower()
                                                        if eye_color not in ["blue", "brown", "green", "hazel"]:
                                                            raise ValueError(f"Invalid eye_color '{eye_color}'")
                                                        # age numeric validation
                                                        age_raw = args.get("age")
                                                        age = int(age_raw)
                                                        if age <= 0 or age > 120:
                                                            raise ValueError("Age out of realistic range")
                                                        ideal_date = str(args.get("ideal_date", "")).strip()
                                                        todays_date = str(args.get("todays_date", "")).strip()
                                                        # Minimal ISO date sanity check
                                                        if len(todays_date) < 8 or todays_date.count('-') != 2:
                                                            raise ValueError("todays_date must be ISO format YYYY-MM-DD")
                                                        result = fill_dating_profile(eye_color, age, ideal_date, todays_date)
                                                        # Update server profile state
                                                        profile_state.update(result)
                                                        function_responses.append({
                                                            "name": name,
                                                            "response": {"result": result},
                                                            "id": call_id
                                                        })
                                                        await client_websocket.send(json.dumps({
                                                            "profile_tool_response": result,
                                                            "text": json.dumps(function_responses)
                                                        }))
                                                        print("Dating profile function executed")
                                                    except Exception as e:
                                                        print(f"Error executing dating profile function: {e}")
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