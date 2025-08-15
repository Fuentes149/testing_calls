"""
app.py — FastAPI + Telnyx Call Control + Asistente de Voz con Gemini con métricas de latencia
REQUISITOS DE ENTORNO (.env):
  TELNYX_API_KEY=...
  TELNYX_CONNECTION_ID=cc-app-xxxxxxxxxxxxxxxxxxxx
  TELNYX_FROM_NUMBER=+1XXXXXXXXXX
  GEMINI_API_KEY=...
  BASE_URL=https://tu-dominio
  # Config para Gemini:
  GEMINI_MODEL=gemini-2.5-flash-preview-native-audio-dialog
  GEMINI_ASSISTANT_GREETING=¡Hola! ¿En qué puedo ayudarte?
  NATIVE_SYSTEM_PROMPT=Eres un agente de soporte...
  # (Opcional) Ajustes VAD y logging
  VAD_RMS_THRESHOLD=500
  VAD_SILENCE_MS=500
  LOG_LEVEL=INFO

EJECUCIÓN LOCAL:
  # Linux/Mac
  PORT=8080 UVICORN_RELOAD=true python app.py
  # PowerShell
  $env:PORT="8080"; $env:UVICORN_RELOAD="true"; python app.py

WEBHOOK:
  POST https://TU-DOMINIO/telnyx-webhook

Notas: pip install fastapi uvicorn websockets python-dotenv requests numpy scipy
"""

import base64
import json
import requests
import audioop
import os
import asyncio
import websockets
import numpy as np
import scipy.signal as signal
import time
import logging

from fastapi import FastAPI, Request, HTTPException, WebSocket
import uvicorn
from dotenv import load_dotenv

# ------------------------
# Config & logging
# ------------------------
load_dotenv()
TELNYX_API_KEY = os.getenv("TELNYX_API_KEY")
TELNYX_CONNECTION_ID = os.getenv("TELNYX_CONNECTION_ID")
TELNYX_FROM_NUMBER = os.getenv("TELNYX_FROM_NUMBER")
BASE_URL = os.getenv("BASE_URL")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-preview-native-audio-dialog")
GEMINI_ASSISTANT_GREETING = os.getenv("GEMINI_ASSISTANT_GREETING", "¡Hola! ¿En qué puedo ayudarte?")
NATIVE_SYSTEM_PROMPT = os.getenv("NATIVE_SYSTEM_PROMPT", "Eres un agente de soporte útil. Responde en español.")

VAD_RMS_THRESHOLD = int(os.getenv("VAD_RMS_THRESHOLD", "500"))   # Umbral de energía
VAD_SILENCE_MS = int(os.getenv("VAD_SILENCE_MS", "500"))         # ms de silencio para fin de habla
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s"
)

# No abortamos el proceso si faltan env vars: lo registramos y validamos en tiempo de uso.
if not BASE_URL:
    logging.warning("BASE_URL no está configurado. Las rutas que construyen STREAM_URL fallarán si se invocan.")
if not GEMINI_API_KEY:
    logging.error("GEMINI_API_KEY no está configurado. No se podrá conectar a Gemini hasta que lo definas.")

app = FastAPI()
sessions = {}

# ------------------------
# Utilidades
# ------------------------
def get_stream_url() -> str:
    """Construye la URL de WebSocket (wss) para /media a partir de BASE_URL."""
    base = os.getenv("BASE_URL")
    if not base:
        # Error en tiempo de uso en lugar de crash al boot
        raise HTTPException(status_code=500, detail="Falta BASE_URL en el entorno del servicio")
    base = base.rstrip("/")
    if base.startswith("https://"):
        return base.replace("https://", "wss://") + "/media"
    elif base.startswith("http://"):
        return base.replace("http://", "ws://") + "/media"
    else:
        # Por defecto asume https→wss
        return "wss://" + base + "/media"

# ------------------------
# Utilidades Latencia
# ------------------------
def now_monotonic():
    """Reloj monotónico en segundos para medir intervalos con precisión."""
    return time.perf_counter()

def ms(dt):
    return round(dt * 1000.0, 1)

def init_latency_tracker():
    return {
        "utt_id": 0,                 # contador de turnos (cada vez que el usuario habla)
        "user_in_speech": False,     # estamos dentro de voz activa del usuario
        "user_start_ts": None,       # inicio de habla
        "user_end_ts": None,         # fin de habla (por silencio)
        "last_voice_ts": None,       # última vez que detectamos energía > umbral
        "awaiting_model": False,     # esperamos primera respuesta del modelo para este turno
        "model_first_audio_ts": None,# primer chunk de audio del modelo para este turno
        "model_end_ts": None,        # fin de la respuesta del modelo (turnComplete)
        "model_in_progress": False,  # modelo está respondiendo
        "interrupted_prev": False    # si el modelo anterior fue interrumpido por habla del usuario
    }

def log_turn_metrics(sid, lat):
    """Log de métricas cuando termina un turno del modelo, si hay datos suficientes."""
    if lat["user_start_ts"] and lat["user_end_ts"]:
        user_speech_ms = ms(lat["user_end_ts"] - lat["user_start_ts"])
    else:
        user_speech_ms = None

    if lat["model_first_audio_ts"] and lat["user_end_ts"]:
        ttfb_ms = ms(lat["model_first_audio_ts"] - lat["user_end_ts"])
    else:
        ttfb_ms = None

    if lat["model_end_ts"] and lat["model_first_audio_ts"]:
        model_speech_ms = ms(lat["model_end_ts"] - lat["model_first_audio_ts"])
    else:
        model_speech_ms = None

    if lat["model_end_ts"] and lat["user_end_ts"]:
        e2e_to_end_ms = ms(lat["model_end_ts"] - lat["user_end_ts"])
    else:
        e2e_to_end_ms = None

    parts = [f"[LATENCY] sid={sid} utt={lat['utt_id']}"]
    if lat["interrupted_prev"]:
        parts.append("MODEL_INTERRUPTED")

    parts.append(f"user_speech_ms={user_speech_ms}")
    parts.append(f"TTFB_ms={ttfb_ms}")
    parts.append(f"model_speech_ms={model_speech_ms}")
    parts.append(f"e2e_to_end_ms={e2e_to_end_ms}")

    logging.info(" ".join(parts))

def reset_for_next_turn(lat):
    """Resetea banderas para un nuevo turno (sin perder contador)."""
    lat["user_in_speech"] = False
    lat["user_start_ts"] = None
    lat["user_end_ts"] = None
    lat["last_voice_ts"] = None
    lat["awaiting_model"] = False
    lat["model_first_audio_ts"] = None
    lat["model_end_ts"] = None
    lat["model_in_progress"] = False
    lat["interrupted_prev"] = False

# ------------------------
# Telnyx helper
# ------------------------
def api(call_id: str, action: str, data: dict = None):
    try:
        if not TELNYX_API_KEY:
            raise HTTPException(status_code=500, detail="Falta TELNYX_API_KEY en el entorno")
        resp = requests.post(
            f"https://api.telnyx.com/v2/calls/{call_id}/actions/{action}",
            headers={"Authorization": f"Bearer {TELNYX_API_KEY}"},
            json=data or {}
        )
        if not resp.ok:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        return resp
    except Exception as ex:
        logging.exception(f"Excepción al enviar comando '{action}': {str(ex)}")
        raise

# ------------------------
# Health / Readiness
# ------------------------
@app.get("/")
def root():
    return {"status": "ok"}

@app.get("/healthz")
def health():
    return {"ok": True}

# ------------------------
# Webhook Telnyx
# ------------------------
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
                sessions[sid] = {
                    "codec": None,
                    "gemini_ws": None,
                    "telnyx_ws": None,
                    "audio_buffer_out": b"",
                    "latency": init_latency_tracker()
                }
                state = base64.b64encode(b"init").decode()
                answer_data = {
                    "client_state": state,
                    "stream_url": get_stream_url(),
                    "stream_track": "both_tracks",
                    "stream_bidirectional_mode": "rtp",
                    "stream_bidirectional_codec": "PCMA"
                }
                api(cid, "answer", answer_data)
                logging.info(f"[Webhook] call.initiated incoming sid={sid}")
            except Exception as ex:
                logging.exception(f"Error al contestar llamada: {str(ex)}")

    elif e["event_type"] == "call.hangup":
        logging.info(f"[Webhook] call.hangup sid={sid}")
        if sid in sessions:
            await close_session(sid)
            del sessions[sid]

    return {"status": "OK"}

# ------------------------
# Click-to-call (outbound)
# ------------------------
@app.post("/make_outbound_call")
async def make_outbound_call(request: Request):
    body = await request.json()
    to_number = body.get("to")
    if not to_number:
        raise HTTPException(status_code=400, detail="Falta 'to'")
    try:
        if not TELNYX_CONNECTION_ID or not TELNYX_FROM_NUMBER or not TELNYX_API_KEY:
            raise HTTPException(status_code=500, detail="Faltan variables TELNYX_* en el entorno")
        state = base64.b64encode(b"init").decode()
        call_data = {
            "connection_id": TELNYX_CONNECTION_ID,
            "to": to_number,
            "from": TELNYX_FROM_NUMBER,
            "client_state": state,
            "stream_url": get_stream_url(),
            "stream_track": "both_tracks",
            "stream_bidirectional_mode": "rtp",
            "stream_bidirectional_codec": "PCMA"
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
            "gemini_ws": None,
            "telnyx_ws": None,
            "audio_buffer_out": b"",
            "latency": init_latency_tracker()
        }
        logging.info(f"[make_outbound_call] sid={sid} -> {to_number}")
        return {"status": "Llamada outbound iniciada", "details": resp.json()}
    except Exception as ex:
        logging.exception(f"Excepción en outbound: {str(ex)}")
        raise

# ------------------------
# Cierre de sesión (sin guardado)
# ------------------------
async def close_session(sid):
    if sid not in sessions:
        return
    sess = sessions[sid]

    # Cierra WS Gemini si está abierto
    if sess.get("gemini_ws"):
        try:
            await sess["gemini_ws"].close()
        except Exception:
            pass

# ------------------------
# Recepción desde Gemini (outbound hacia Telnyx)
# ------------------------
async def receive_from_gemini(sid):
    sess = sessions[sid]
    gemini_ws = sess["gemini_ws"]
    telnyx_ws = sess["telnyx_ws"]
    codec = sess["codec"]
    lat = sess["latency"]

    try:
        async for message in gemini_ws:
            data = json.loads(message)

            # 1) Interrupción del modelo (usuario habló encima)
            try:
                interrupted = data["serverContent"]["interrupted"]
                if interrupted:
                    if lat["model_in_progress"]:
                        lat["interrupted_prev"] = True
                        lat["model_end_ts"] = now_monotonic()
                        log_turn_metrics(sid, lat)
                        lat["model_in_progress"] = False
                        lat["awaiting_model"] = False
                        lat["model_first_audio_ts"] = None
                # limpiamos buffer de salida para evitar eco parcial
                sess["audio_buffer_out"] = b""
            except KeyError:
                pass

            # 2) Llega audio del modelo (primer chunk => medir TTFB)
            try:
                audio_b64 = data["serverContent"]["modelTurn"]["parts"][0]["inlineData"]["data"]
                audio_delta = base64.b64decode(audio_b64)
                if audio_delta:
                    # Si estábamos esperando la respuesta del modelo para este turno, medimos TTFB
                    if lat["awaiting_model"] and not lat["model_in_progress"]:
                        lat["model_first_audio_ts"] = now_monotonic()
                        lat["model_in_progress"] = True
                        lat["awaiting_model"] = False
                        logging.debug(
                            f"[Gemini] first audio sid={sid} utt={lat['utt_id']} "
                            f"TTFB_ms={ms(lat['model_first_audio_ts'] - lat['user_end_ts']) if lat['user_end_ts'] else None}"
                        )

                    sess["audio_buffer_out"] += audio_delta

                    # Salida de Gemini llega a 24k PCM16; troceamos en ~20 ms
                    chunk_size = 960  # 20 ms @ 24kHz mono 16-bit -> 24_000 * 0.02 * 2 = 960
                    while len(sess["audio_buffer_out"]) >= chunk_size:
                        pcm24k = sess["audio_buffer_out"][:chunk_size]
                        sess["audio_buffer_out"] = sess["audio_buffer_out"][chunk_size:]

                        # Filtro y downsample a 8k para Telnyx
                        b, a = signal.butter(4, 3900 / (24000 / 2), btype='low')
                        pcm24k_np = np.frombuffer(pcm24k, dtype=np.int16).astype(np.float32)
                        if len(pcm24k_np) > 14:
                            filtered_np = signal.filtfilt(b, a, pcm24k_np)
                        else:
                            filtered_np = pcm24k_np
                        pcm24k_filtered = filtered_np.astype(np.int16)

                        pcm8k_np = signal.resample(pcm24k_filtered, int(len(pcm24k_filtered) * 8000 / 24000))
                        pcm8k = pcm8k_np.astype(np.int16).tobytes()

                        if codec == "PCMA":
                            encoded_bytes = audioop.lin2alaw(pcm8k, 2)
                        elif codec == "PCMU":
                            encoded_bytes = audioop.lin2ulaw(pcm8k, 2)
                        else:
                            # Codec no soportado para envío a Telnyx
                            continue

                        media_event = {
                            "event": "media",
                            "media": {
                                "payload": base64.b64encode(encoded_bytes).decode(),
                                "track": "outbound"
                            }
                        }
                        await telnyx_ws.send_text(json.dumps(media_event))
            except KeyError:
                pass

            # 3) Fin del turno del modelo => cerramos métricas y log
            try:
                turn_complete = data["serverContent"]["turnComplete"]
                if turn_complete:
                    lat["model_end_ts"] = now_monotonic()
                    log_turn_metrics(sid, lat)
                    # preparamos para el siguiente turno
                    lat["model_in_progress"] = False
                    lat["awaiting_model"] = False
                    lat["model_first_audio_ts"] = None
                    lat["model_end_ts"] = None
                    lat["interrupted_prev"] = False
            except KeyError:
                pass

    except Exception as ex:
        logging.exception(f"Error recibiendo de Gemini: {str(ex)}")

# ------------------------
# WebSocket bidireccional con Telnyx
# ------------------------
@app.websocket("/media")
async def handle_media_stream(websocket: WebSocket):
    await websocket.accept()
    sid = None
    gemini_task = None

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

                    # Conectar a Gemini Realtime API (si hay API key)
                    if not GEMINI_API_KEY:
                        logging.error("[WS] GEMINI_API_KEY ausente: no se puede conectar a Gemini.")
                    else:
                        uri = (
                            "wss://generativelanguage.googleapis.com/ws/"
                            "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
                            f"?key={GEMINI_API_KEY}"
                        )
                        try:
                            gemini_ws = await websockets.connect(uri, max_size=None)
                            sessions[sid]["gemini_ws"] = gemini_ws
                            logging.info(f"[WS] Gemini conectado sid={sid}")
                        except Exception as ex:
                            logging.exception(f"Error conectando a Gemini: {str(ex)}")

                    # Configurar sesión en Gemini (AAD activado con defaults seguros)
                    if sessions[sid].get("gemini_ws"):
                        system_prompt = (
                            NATIVE_SYSTEM_PROMPT
                            + f"\nCuando el usuario diga '___start___', responde exactamente: '{GEMINI_ASSISTANT_GREETING}' y espera su consulta."
                        )
                        setup_event = {
                            "setup": {
                                "model": GEMINI_MODEL,
                                "generationConfig": {
                                    "responseModalities": ["AUDIO"],
                                    "speechConfig": {
                                        "voiceConfig": {
                                            "prebuiltVoiceConfig": {"voiceName": "Puck"}
                                        }
                                    }
                                },
                                "systemInstruction": {"parts": [{"text": system_prompt}]},
                                "realtimeInputConfig": {
                                    "automaticActivityDetection": {
                                        "disabled": False
                                    }
                                }
                            }
                        }
                        await sessions[sid]["gemini_ws"].send(json.dumps(setup_event))
                        await sessions[sid]["gemini_ws"].recv()  # Esperar ACK

                        # Trigger saludo inicial
                        initial_input = {"realtime_input": {"text": "___start___"}}
                        await sessions[sid]["gemini_ws"].send(json.dumps(initial_input))

                        # Iniciar tarea de recepción de Gemini
                        gemini_task = asyncio.create_task(receive_from_gemini(sid))
                    logging.info(f"[Telnyx WS] start sid={sid} codec={codec}")

            elif evt == "media" and sid in sessions:
                media_info = data.get("media", {})
                track = media_info.get("track")
                payload = media_info.get("payload")
                if not payload or track != "inbound":
                    continue

                raw = base64.b64decode(payload)
                sess = sessions[sid]
                codec = sess["codec"]
                gemini_ws = sess["gemini_ws"]
                lat = sess["latency"]

                if codec in ["PCMU", "PCMA"]:
                    # Decodificar a PCM16 8k mono
                    if codec == "PCMA":
                        pcm = audioop.alaw2lin(raw, 2)   # 8k mono 16-bit
                    else:
                        pcm = audioop.ulaw2lin(raw, 2)

                    # --------------------
                    # VAD: detección de inicio/fin de habla
                    # --------------------
                    rms = audioop.rms(pcm, 2)  # energía
                    now_t = now_monotonic()

                    if rms >= VAD_RMS_THRESHOLD:
                        if not lat["user_in_speech"]:
                            lat["utt_id"] += 1
                            lat["user_in_speech"] = True
                            lat["user_start_ts"] = now_t
                            lat["user_end_ts"] = None
                            lat["awaiting_model"] = False
                            lat["model_first_audio_ts"] = None
                            lat["model_end_ts"] = None
                            lat["model_in_progress"] = False
                            lat["interrupted_prev"] = False
                            logging.debug(f"[VAD] USER_START sid={sid} utt={lat['utt_id']}")
                        lat["last_voice_ts"] = now_t
                    else:
                        if lat["user_in_speech"] and lat["last_voice_ts"]:
                            if (now_t - lat["last_voice_ts"]) * 1000.0 >= VAD_SILENCE_MS:
                                lat["user_in_speech"] = False
                                lat["user_end_ts"] = lat["last_voice_ts"]
                                lat["awaiting_model"] = True   # ahora esperamos la 1ª respuesta del modelo
                                logging.debug(
                                    f"[VAD] USER_END sid={sid} utt={lat['utt_id']} "
                                    f"user_speech_ms={ms(lat['user_end_ts'] - lat['user_start_ts'])}"
                                )

                    # Low-pass + resample a 16k para Gemini
                    pcm8k_np = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
                    b, a = signal.butter(4, 3900 / (8000 / 2), btype='low')
                    if len(pcm8k_np) > 14:
                        filtered_np = signal.filtfilt(b, a, pcm8k_np)
                    else:
                        filtered_np = pcm8k_np
                    pcm8k_filtered = filtered_np.astype(np.int16)
                    pcm16k_np = signal.resample(pcm8k_filtered, int(len(pcm8k_filtered) * 16000 / 8000))
                    pcm16k = pcm16k_np.astype(np.int16).tobytes()

                    # Enviar a Gemini (si hay WS)
                    if gemini_ws:
                        append_event = {
                            "realtime_input": {
                                "audio": {
                                    "data": base64.b64encode(pcm16k).decode(),
                                    "mime_type": "audio/pcm;rate=16000"
                                }
                            }
                        }
                        await gemini_ws.send(json.dumps(append_event))
                else:
                    # Codec no soportado para este flujo
                    logging.warning(f"[MEDIA] Codec no soportado inbound: {codec}")

            elif evt == "stop":
                logging.info(f"[Telnyx WS] stop sid={sid}")
                if sid in sessions:
                    await close_session(sid)
                break

    except Exception as ex:
        logging.exception(f"Error en WebSocket: {str(ex)}")
    finally:
        if gemini_task:
            gemini_task.cancel()
        try:
            await websocket.close()
        except Exception:
            pass

# ------------------------
# Main
# ------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))  # Cloud Run define PORT; 8080 por defecto local
    reload_flag = os.getenv("UVICORN_RELOAD", "false").lower() == "true"
    logging.info(f"[BOOT] Iniciando Uvicorn en 0.0.0.0:{port} (reload={reload_flag})")
    uvicorn.run(app, host="0.0.0.0", port=port, reload=reload_flag)
