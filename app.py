# app.py
# FastAPI + Telnyx Call Control + Gemini Realtime (SIN guardar audios, SIN SciPy)
# Requisitos: fastapi, uvicorn[standard], websockets, requests, numpy, python-dotenv

import os
import json
import base64
import time
import logging
from datetime import datetime

import requests
import numpy as np
import audioop
import asyncio
import websockets

from fastapi import FastAPI, Request, HTTPException, WebSocket
from dotenv import load_dotenv

# ------------------------
# Config & logging
# ------------------------
load_dotenv()

TELNYX_API_KEY = os.getenv("TELNYX_API_KEY")
TELNYX_CONNECTION_ID = os.getenv("TELNYX_CONNECTION_ID")
TELNYX_FROM_NUMBER = os.getenv("TELNYX_FROM_NUMBER")
BASE_URL = os.getenv("BASE_URL")  # https://tu-dominio
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-preview-native-audio-dialog")
GEMINI_ASSISTANT_GREETING = os.getenv("GEMINI_ASSISTANT_GREETING", "¡Hola! ¿En qué puedo ayudarte?")
NATIVE_SYSTEM_PROMPT = os.getenv("NATIVE_SYSTEM_PROMPT", "Eres un agente de soporte útil. Responde en español.")
VAD_RMS_THRESHOLD = int(os.getenv("VAD_RMS_THRESHOLD", "500"))
VAD_SILENCE_MS = int(os.getenv("VAD_SILENCE_MS", "500"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s"
)

# No abortar al arrancar si faltan ENV; solo avisar.
missing = [k for k, v in {
    "BASE_URL": BASE_URL,
    "GEMINI_API_KEY": GEMINI_API_KEY,
}.items() if not v]
if missing:
    logging.warning(f"Faltan variables de entorno: {missing}. La app arranca igual, "
                    "pero fallará al usarlas.")

def get_stream_url() -> str:
    """Convierte BASE_URL https -> wss para el websocket /media."""
    if not BASE_URL or not BASE_URL.startswith("https://"):
        raise HTTPException(status_code=500, detail="BASE_URL debe ser https://... (ej. https://mi-dominio)")
    return BASE_URL.replace("https://", "wss://") + "/media"


# ------------------------
# Utilidades de audio (sin SciPy)
# ------------------------
def downsample_24k_to_8k(pcm24_bytes: bytes) -> bytes:
    """Baja de 24 kHz a 8 kHz por decimación x3 (simple y rápido)."""
    x = np.frombuffer(pcm24_bytes, dtype=np.int16)
    if x.size == 0:
        return b""
    y = x[::3]  # toma 1 de cada 3 muestras
    return y.astype(np.int16).tobytes()

def upsample_8k_to_16k_linear(pcm8_bytes: bytes) -> bytes:
    """Sube de 8 kHz a 16 kHz por interpolación lineal x2."""
    x = np.frombuffer(pcm8_bytes, dtype=np.int16)
    n = x.size
    if n == 0:
        return b""
    if n == 1:
        # duplica la única muestra
        y = np.repeat(x, 2)
        return y.astype(np.int16).tobytes()

    y = np.empty(n * 2, dtype=np.int16)
    y[0::2] = x  # índices pares = originales
    # índices impares = promedio de vecinos
    mid = (x[:-1].astype(np.int32) + x[1:].astype(np.int32)) // 2
    y[1:-1:2] = mid.astype(np.int16)
    y[-1] = x[-1]  # último impar replica el último valor
    return y.tobytes()


# ------------------------
# Latencia helpers
# ------------------------
def now_monotonic():
    return time.perf_counter()

def ms(dt):
    return round(dt * 1000.0, 1)

def init_latency_tracker():
    return {
        "utt_id": 0,
        "user_in_speech": False,
        "user_start_ts": None,
        "user_end_ts": None,
        "last_voice_ts": None,
        "awaiting_model": False,
        "model_first_audio_ts": None,
        "model_end_ts": None,
        "model_in_progress": False,
        "interrupted_prev": False
    }

def log_turn_metrics(sid, lat):
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
    parts += [
        f"user_speech_ms={user_speech_ms}",
        f"TTFB_ms={ttfb_ms}",
        f"model_speech_ms={model_speech_ms}",
        f"e2e_to_end_ms={e2e_to_end_ms}",
    ]
    logging.info(" ".join(parts))


# ------------------------
# App & estado en memoria
# ------------------------
app = FastAPI()
sessions = {}  # sid -> {codec, gemini_ws, telnyx_ws, audio_buffer_out, latency}


# ------------------------
# Telnyx helpers
# ------------------------
def telnyx_action(call_id: str, action: str, data: dict = None):
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
        logging.exception(f"Excepción al enviar comando '{action}': {str(ex)}")
        raise


# ------------------------
# Healthcheck
# ------------------------
@app.get("/healthz")
def healthz():
    return {"ok": True, "time": datetime.utcnow().isoformat()}


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
                    "latency": init_latency_tracker(),
                }
                state = base64.b64encode(b"init").decode()
                answer_data = {
                    "client_state": state,
                    "stream_url": get_stream_url(),
                    "stream_track": "both_tracks",
                    "stream_bidirectional_mode": "rtp",
                    "stream_bidirectional_codec": "PCMA"
                }
                telnyx_action(cid, "answer", answer_data)
                logging.info(f"[Webhook] call.initiated incoming sid={sid}")
            except Exception as ex:
                logging.exception(f"Error al contestar llamada: {str(ex)}")

    elif e["event_type"] == "call.hangup":
        logging.info(f"[Webhook] call.hangup sid={sid}")
        if sid in sessions:
            await close_session(sid)
            sessions.pop(sid, None)

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
            "latency": init_latency_tracker(),
        }
        logging.info(f"[make_outbound_call] sid={sid} -> {to_number}")
        return {"status": "Llamada outbound iniciada", "details": resp.json()}
    except Exception as ex:
        logging.exception(f"Excepción en outbound: {str(ex)}")
        raise


# ------------------------
# Cierre de sesión (sin archivos)
# ------------------------
async def close_session(sid):
    sess = sessions.get(sid)
    if not sess:
        return
    if sess.get("gemini_ws"):
        try:
            await sess["gemini_ws"].close()
        except Exception:
            pass


# ------------------------
# Recibir desde Gemini -> enviar a Telnyx
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

            # 1) Interrupción
            try:
                interrupted = data["serverContent"]["interrupted"]
                if interrupted and lat["model_in_progress"]:
                    lat["interrupted_prev"] = True
                    lat["model_end_ts"] = now_monotonic()
                    log_turn_metrics(sid, lat)
                    lat["model_in_progress"] = False
                    lat["awaiting_model"] = False
                    lat["model_first_audio_ts"] = None
                    sess["audio_buffer_out"] = b""
            except KeyError:
                pass

            # 2) Audio del modelo (24k PCM16 mono)
            try:
                audio_b64 = data["serverContent"]["modelTurn"]["parts"][0]["inlineData"]["data"]
                audio_delta = base64.b64decode(audio_b64)
                if audio_delta:
                    if lat["awaiting_model"] and not lat["model_in_progress"]:
                        lat["model_first_audio_ts"] = now_monotonic()
                        lat["model_in_progress"] = True
                        lat["awaiting_model"] = False
                        logging.debug(
                            f"[Gemini] first audio sid={sid} utt={lat['utt_id']} "
                            f"TTFB_ms={ms(lat['model_first_audio_ts'] - lat['user_end_ts']) if lat['user_end_ts'] else None}"
                        )

                    sess["audio_buffer_out"] += audio_delta

                    # Procesar en chunks de 20ms @24k (960 bytes)
                    chunk_size = 960
                    while len(sess["audio_buffer_out"]) >= chunk_size:
                        pcm24k = sess["audio_buffer_out"][:chunk_size]
                        sess["audio_buffer_out"] = sess["audio_buffer_out"][chunk_size:]

                        # Downsample 24k -> 8k (simple decimación)
                        pcm8k = downsample_24k_to_8k(pcm24k)

                        # Codificar a PCMA/PCMU para Telnyx
                        if codec == "PCMA":
                            encoded = audioop.lin2alaw(pcm8k, 2)
                        elif codec == "PCMU":
                            encoded = audioop.lin2ulaw(pcm8k, 2)
                        else:
                            continue

                        media_event = {
                            "event": "media",
                            "media": {
                                "payload": base64.b64encode(encoded).decode(),
                                "track": "outbound"
                            }
                        }
                        await telnyx_ws.send_text(json.dumps(media_event))
            except KeyError:
                pass

            # 3) Fin del turno del modelo
            try:
                if data["serverContent"]["turnComplete"]:
                    lat["model_end_ts"] = now_monotonic()
                    log_turn_metrics(sid, lat)
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

                    # Conectar a Gemini
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
                        continue

                    # Setup Gemini (AAD activado)
                    system_prompt = (
                        NATIVE_SYSTEM_PROMPT
                        + f"\nCuando el usuario diga '___start___', responde exactamente: '{GEMINI_ASSISTANT_GREETING}' y espera su consulta."
                    )
                    setup_event = {
                        "setup": {
                            "model": GEMINI_MODEL,
                            "generationConfig": {
                                "responseModalities": ["AUDIO"],
                                "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Puck"}}}
                            },
                            "systemInstruction": {"parts": [{"text": system_prompt}]},
                            "realtimeInputConfig": {"automaticActivityDetection": {"disabled": False}}
                        }
                    }
                    await gemini_ws.send(json.dumps(setup_event))
                    await gemini_ws.recv()  # ACK

                    # Saludo inicial
                    initial_input = {"realtime_input": {"text": "___start___"}}
                    await gemini_ws.send(json.dumps(initial_input))

                    # Tarea para recibir audio de Gemini
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
                    # 8 kHz mono, 16-bit PCM
                    pcm = audioop.alaw2lin(raw, 2) if codec == "PCMA" else audioop.ulaw2lin(raw, 2)

                    # VAD (inicio/fin de habla)
                    rms = audioop.rms(pcm, 2)
                    now = now_monotonic()
                    if rms >= VAD_RMS_THRESHOLD:
                        if not lat["user_in_speech"]:
                            lat["utt_id"] += 1
                            lat["user_in_speech"] = True
                            lat["user_start_ts"] = now
                            lat["user_end_ts"] = None
                            lat["awaiting_model"] = False
                            lat["model_first_audio_ts"] = None
                            lat["model_end_ts"] = None
                            lat["model_in_progress"] = False
                            lat["interrupted_prev"] = False
                            logging.debug(f"[VAD] USER_START sid={sid} utt={lat['utt_id']}")
                        lat["last_voice_ts"] = now
                    else:
                        if lat["user_in_speech"] and lat["last_voice_ts"]:
                            if (now - lat["last_voice_ts"]) * 1000.0 >= VAD_SILENCE_MS:
                                lat["user_in_speech"] = False
                                lat["user_end_ts"] = lat["last_voice_ts"]
                                lat["awaiting_model"] = True
                                logging.debug(f"[VAD] USER_END sid={sid} utt={lat['utt_id']} "
                                              f"user_speech_ms={ms(lat['user_end_ts'] - lat['user_start_ts'])}")

                    # 8k -> 16k para Gemini (interpolación x2)
                    pcm16k = upsample_8k_to_16k_linear(pcm)

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
                    # Otros codecs no manejados
                    pass

            elif evt == "stop":
                logging.info(f"[Telnyx WS] stop sid={sid}")
                if sid in sessions:
                    await close_session(sid)
                    sessions.pop(sid, None)
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
    port = int(os.getenv("PORT", "8080"))
    reload = os.getenv("RELOAD", "0") == "1"  # para pruebas locales
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=port, log_level="info", reload=reload)
