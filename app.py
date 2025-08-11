import base64
import json
import requests
from datetime import datetime
import wave
import audioop
import os
from pydub import AudioSegment  # Requiere: pip install pydub y tener ffmpeg instalado en el sistema
import asyncio
import websockets
import numpy as np
import scipy.signal as signal

from fastapi import FastAPI, Request, HTTPException, WebSocket
import uvicorn
from dotenv import load_dotenv

load_dotenv()
TELNYX_API_KEY = os.getenv("TELNYX_API_KEY")
TELNYX_CONNECTION_ID = os.getenv("TELNYX_CONNECTION_ID")
TELNYX_FROM_NUMBER = os.getenv("TELNYX_FROM_NUMBER")
BASE_URL = os.getenv("BASE_URL")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not BASE_URL:
    raise ValueError("BASE_URL no está configurado en las variables de entorno")

if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY no está configurado en las variables de entorno")

STREAM_URL = BASE_URL.replace("https://", "wss://") + "/media"

app = FastAPI()

sessions = {}

def api(call_id: str, action: str, data: dict = None):
    try:
        resp = requests.post(
            f"https://api.telnyx.com/v2/calls/{call_id}/actions/{action}",
            headers={"Authorization": f"Bearer {TELNYX_API_KEY}"},
            json=data or {}
        )
        if not resp.ok:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        return resp
    except Exception as ex:
        print(f"Excepción al enviar comando '{action}': {str(ex)}")
        raise

@app.post("/telnyx-webhook")
async def telnyx_webhook(request: Request):
    data = await request.json()
    e = data["data"]
    p = e["payload"]
    cid = p.get("call_control_id")
    sid = p.get("call_session_id")
    direction = p.get("direction", "unknown")

    if e["event_type"] == "call.initiated":
        if direction == "incoming":
            try:
                # Inicializar sesión (codec se setea en WS start)
                sessions[sid] = {
                    "codec": None,
                    "raw_bytes": b"",
                    "wavefile": None,
                    "filename": f"llamadas_guardadas/inbound_call_{datetime.now().strftime('%Y%m%d_%H%M%S')}.wav",
                    "openai_ws": None,
                    "telnyx_ws": None,
                    "audio_buffer_out": b"",
                    "output_pcm": b""  # Para depuración: acumular PCM 8kHz antes de codificar
                }

                state = base64.b64encode(b"init").decode()
                answer_data = {
                    "client_state": state,
                    "stream_url": STREAM_URL,
                    "stream_track": "both_tracks",
                    "stream_bidirectional_mode": "rtp",
                    "stream_bidirectional_codec": "PCMA"  # Cambiado a PCMA para coincidir con el codec detectado en tus logs
                }
                api(cid, "answer", answer_data)
            except Exception as ex:
                print(f"Error al contestar llamada: {str(ex)}")


    elif e["event_type"] == "call.hangup":
        if sid in sessions:
            await close_session(sid)
            del sessions[sid]

    return {"status": "OK"}

@app.post("/make_outbound_call")
async def make_outbound_call(request: Request):
    body = await request.json()
    to_number = body.get("to")
    if not to_number:
        raise HTTPException(status_code=400, detail="Falta 'to'")
    try:
        state = base64.b64encode(b"init").decode()
        call_data = {
            "connection_id": TELNYX_CONNECTION_ID,
            "to": to_number,
            "from": TELNYX_FROM_NUMBER,
            "client_state": state,
            "stream_url": STREAM_URL,
            "stream_track": "both_tracks",
            "stream_bidirectional_mode": "rtp",
            "stream_bidirectional_codec": "PCMA"  # Cambiado a PCMA
        }
        resp = requests.post(
            "https://api.telnyx.com/v2/calls",
            headers={"Authorization": f"Bearer {TELNYX_API_KEY}", "Content-Type": "application/json"},
            json=call_data
        )
        if not resp.ok:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        
        resp_data = resp.json()["data"]
        sid = resp_data.get("call_session_id")
        if not sid:
            raise HTTPException(status_code=500, detail="No se obtuvo call_session_id")
        
        sessions[sid] = {
            "codec": None,
            "raw_bytes": b"",
            "wavefile": None,
            "filename": f"llamadas_guardadas/outbound_call_{datetime.now().strftime('%Y%m%d_%H%M%S')}.wav",
            "openai_ws": None,
            "telnyx_ws": None,
            "audio_buffer_out": b"",
            "output_pcm": b""
        }
        
        return {"status": "Llamada outbound iniciada", "details": resp.json()}
    except Exception as ex:
        print(f"Excepción en outbound: {str(ex)}")
        raise

async def close_session(sid):
    if sid not in sessions:
        return
    sess = sessions[sid]
    codec = sess["codec"]
    filename = sess["filename"]

    if codec in ["PCMU", "PCMA"]:
        if sess["wavefile"]:
            sess["wavefile"].close()
    else:
        # Para OPUS/G722, guardar raw y convertir con pydub
        if sess["raw_bytes"]:
            ext = "opus" if codec == "OPUS" else "g722" if codec == "G722" else "raw"
            temp_path = f"temp_{sid}.{ext}"
            with open(temp_path, "wb") as f:
                f.write(sess["raw_bytes"])
            audio = AudioSegment.from_file(temp_path, format=ext)
            audio.export(filename, format="wav")
            os.remove(temp_path)
            print(f"Grabación convertida y guardada en {filename}")

    # Cerrar conexión OpenAI si existe
    if sess["openai_ws"]:
        await sess["openai_ws"].close()

    # Para depuración: Guardar audio de salida como WAV
    if sess["output_pcm"]:
        output_filename = f"llamadas_guardadas/output_audio_{datetime.now().strftime('%Y%m%d_%H%M%S')}.wav"
        wf = wave.open(output_filename, "wb")
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(8000)
        wf.writeframes(sess["output_pcm"])
        wf.close()
        print(f"Audio de salida de OpenAI guardado en {output_filename} para depuración")

async def receive_from_openai(sid):
    sess = sessions[sid]
    openai_ws = sess["openai_ws"]
    telnyx_ws = sess["telnyx_ws"]
    codec = sess["codec"]
    try:
        async for message in openai_ws:
            data = json.loads(message)
            event_type = data.get("type")
            print(f"Evento recibido de OpenAI: {json.dumps(data)}")  # Depuración detallada

            if event_type == "response.audio.delta":
                audio_delta = base64.b64decode(data.get("delta", ""))
                print(f"Recibido audio delta de longitud: {len(audio_delta)}")  # Depuración
                if audio_delta:
                    # Acumular audio de salida (PCM16 24kHz)
                    sess["audio_buffer_out"] += audio_delta
                    # Procesar en chunks de 20ms para coincidir con ptime de Telnyx
                    chunk_size = 960  # 24000 Hz * 2 bytes * 0.02 s
                    while len(sess["audio_buffer_out"]) >= chunk_size:
                        pcm24k = sess["audio_buffer_out"][:chunk_size]
                        sess["audio_buffer_out"] = sess["audio_buffer_out"][chunk_size:]

                        # Low-pass filter antes de resample (orden 4, padlen ~14)
                        b, a = signal.butter(4, 3900 / (24000 / 2), btype='low')
                        pcm24k_np = np.frombuffer(pcm24k, dtype=np.int16).astype(np.float32)
                        if len(pcm24k_np) > 14:
                            filtered_np = signal.filtfilt(b, a, pcm24k_np)
                        else:
                            filtered_np = pcm24k_np
                        pcm24k_filtered = filtered_np.astype(np.int16)

                        # Resample a 8kHz
                        pcm8k_np = signal.resample(pcm24k_filtered, int(len(pcm24k_filtered) * 8000 / 24000))
                        pcm8k = pcm8k_np.astype(np.int16).tobytes()

                        # Acumular para guardar WAV de depuración
                        sess["output_pcm"] += pcm8k

                        # Codificar según el codec detectado
                        if codec == "PCMA":
                            encoded_bytes = audioop.lin2alaw(pcm8k, 2)
                        elif codec == "PCMU":
                            encoded_bytes = audioop.lin2ulaw(pcm8k, 2)
                        else:
                            print(f"Codec no soportado para salida: {codec}")
                            continue

                        print(f"Enviando chunk codificado de longitud: {len(encoded_bytes)}")  # Depuración

                        # Enviar a Telnyx
                        media_event = {
                            "event": "media",
                            "media": {
                                "payload": base64.b64encode(encoded_bytes).decode(),
                                "track": "outbound"
                            }
                        }
                        await telnyx_ws.send_text(json.dumps(media_event))
                        print("Enviado chunk de audio a Telnyx")  # Para depuración

            elif event_type == "response.audio.done":
                # Enviar cualquier buffer restante
                if sess["audio_buffer_out"]:
                    pcm24k = sess["audio_buffer_out"]
                    sess["audio_buffer_out"] = b""
                    b, a = signal.butter(4, 3900 / (24000 / 2), btype='low')
                    pcm24k_np = np.frombuffer(pcm24k, dtype=np.int16).astype(np.float32)
                    if len(pcm24k_np) > 14:
                        filtered_np = signal.filtfilt(b, a, pcm24k_np)
                    else:
                        filtered_np = pcm24k_np
                    pcm24k_filtered = filtered_np.astype(np.int16)
                    pcm8k_np = signal.resample(pcm24k_filtered, int(len(pcm24k_filtered) * 8000 / 24000))
                    pcm8k = pcm8k_np.astype(np.int16).tobytes()
                    sess["output_pcm"] += pcm8k
                    if codec == "PCMA":
                        encoded_bytes = audioop.lin2alaw(pcm8k, 2)
                    elif codec == "PCMU":
                        encoded_bytes = audioop.lin2ulaw(pcm8k, 2)
                    else:
                        print(f"Codec no soportado para salida: {codec}")
                        return
                    print(f"Enviando buffer restante codificado de longitud: {len(encoded_bytes)}")  # Depuración
                    media_event = {
                        "event": "media",
                        "media": {
                            "payload": base64.b64encode(encoded_bytes).decode(),
                            "track": "outbound"
                        }
                    }
                    await telnyx_ws.send_text(json.dumps(media_event))
                    print("Enviado buffer restante de audio a Telnyx")  # Para depuración

            # Manejar otros eventos si es necesario, como transcripciones o texto
            if event_type in ["response.audio_transcript.delta", "response.content_part.done", "input_audio_buffer.speech_started", "input_audio_buffer.speech_stopped"]:
                print(f"Evento OpenAI específico: {json.dumps(data)}")  # Más depuración

    except Exception as ex:
        print(f"Error recibiendo de OpenAI: {str(ex)}")

@app.websocket("/media")
async def handle_media_stream(websocket: WebSocket):
    await websocket.accept()
    sid = None
    openai_task = None
    try:
        while True:
            msg = await websocket.receive_text()
            data = json.loads(msg)
            evt = data.get("event")

            if evt == "start":
                start_info = data.get("start", {})
                sid = start_info.get("call_session_id")
                codec = start_info.get("media_format", {}).get("encoding")
                if sid in sessions:
                    sessions[sid]["codec"] = codec
                    sessions[sid]["telnyx_ws"] = websocket
                    print(f"Codec detectado: {codec}")  # Para depuración
                    if codec in ["PCMU", "PCMA"]:
                        # Para narrowband, inicializar WAV a 8kHz
                        wf = wave.open(sessions[sid]["filename"], "wb")
                        wf.setnchannels(1)
                        wf.setsampwidth(2)
                        wf.setframerate(8000)
                        sessions[sid]["wavefile"] = wf
                    # Para wideband, usamos raw_bytes y convertimos al final (pydub maneja rates más altos)

                    # Conectar a OpenAI Realtime API
                    url = "wss://api.openai.com/v1/realtime?model=gpt-4o-mini-realtime-preview-2024-12-17"
                    headers = {
                        "Authorization": f"Bearer {OPENAI_API_KEY}",
                        "OpenAI-Beta": "realtime=v1"
                    }
                    try:
                        openai_ws = await websockets.connect(url, extra_headers=headers)
                        sessions[sid]["openai_ws"] = openai_ws
                        print("Conectado exitosamente a OpenAI Realtime API")  # Depuración
                    except Exception as ex:
                        print(f"Error conectando a OpenAI: {str(ex)}")
                        continue

                    # Configurar sesión en OpenAI con VAD thresholds
                    session_event = {
                        "type": "session.update",
                        "session": {
                            "modalities": ["text", "audio"],
                            "instructions": "Eres un asistente útil. Responde en español ya que el usuario habla en español.",
                            "voice": "alloy",
                            "input_audio_format": "pcm16",
                            "output_audio_format": "pcm16",
                            "turn_detection": {
                                "type": "server_vad",
                                "threshold": 0.8,
                                "prefix_padding_ms": 300,
                                "silence_duration_ms": 200
                            },
                            "temperature": 0.8,
                        }
                    }
                    await openai_ws.send(json.dumps(session_event))
                    print("Enviada configuración de sesión a OpenAI")  # Depuración

                    # Para que el agente hable primero, crear una respuesta inicial
                    initial_response = {
                        "type": "response.create",
                        "response": {
                            "modalities": ["text", "audio"],
                            "instructions": "Saluda al usuario en español y pregúntale cómo puedes ayudarle hoy."
                        }
                    }
                    await openai_ws.send(json.dumps(initial_response))
                    print("Enviada solicitud de respuesta inicial a OpenAI")  # Depuración

                    # Iniciar tarea para recibir de OpenAI
                    openai_task = asyncio.create_task(receive_from_openai(sid))

            elif evt == "media" and sid in sessions:
                media_info = data.get("media", {})
                track = media_info.get("track")
                payload = media_info.get("payload")
                if not payload or track != "inbound":
                    continue  # Ignorar outbound por ahora

                raw = base64.b64decode(payload)
                sess = sessions[sid]
                codec = sess["codec"]
                openai_ws = sess["openai_ws"]

                if codec in ["PCMU", "PCMA"]:
                    if codec == "PCMA":
                        pcm = audioop.alaw2lin(raw, 2)
                    elif codec == "PCMU":
                        pcm = audioop.ulaw2lin(raw, 2)
                    # Low-pass filter antes de resample
                    pcm8k_np = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
                    b, a = signal.butter(4, 3900 / (8000 / 2), btype='low')
                    if len(pcm8k_np) > 14:
                        filtered_np = signal.filtfilt(b, a, pcm8k_np)
                    else:
                        filtered_np = pcm8k_np
                    pcm8k_filtered = filtered_np.astype(np.int16)
                    pcm24k_np = signal.resample(pcm8k_filtered, int(len(pcm8k_filtered) * 24000 / 8000))
                    pcm24k = pcm24k_np.astype(np.int16).tobytes()

                    # Enviar a OpenAI
                    if openai_ws:
                        append_event = {
                            "type": "input_audio_buffer.append",
                            "audio": base64.b64encode(pcm24k).decode()
                        }
                        await openai_ws.send(json.dumps(append_event))
                        print("Enviado audio inbound a OpenAI")  # Depuración

                    # Continuar grabando
                    sess["wavefile"].writeframes(pcm)
                else:
                    # Para OPUS/G722, acumular raw bytes (lógica de grabación original)
                    sess["raw_bytes"] += raw
                    # Para integración con OpenAI, se requeriría decodificar OPUS/G722 a PCM y resamplear.
                    # Esto requiere librerías adicionales como pyopus o usar pydub en chunks con BytesIO.
                    # Por simplicidad, usamos PCMU/PCMA. Si se necesita OPUS, agregar decodificación aquí.

            elif evt == "stop":
                if sid in sessions:
                    await close_session(sid)
                break
    except Exception as ex:
        print(f"Error en WebSocket: {str(ex)}")
    finally:
        if openai_task:
            openai_task.cancel()
        await websocket.close()

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=5000, reload=True)