import base64
import json
import requests
from datetime import datetime

from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
import uvicorn
from dotenv import load_dotenv
import os
import wave
import audioop
from pathlib import Path

# Carga variables de entorno
load_dotenv()
TELNYX_API_KEY = os.getenv("TELNYX_API_KEY")
TELNYX_CONNECTION_ID = os.getenv("TELNYX_CONNECTION_ID")
TELNYX_FROM_NUMBER = os.getenv("TELNYX_FROM_NUMBER")
# Asegúrate de configurar STREAM_URL en .env como wss://tu-app.onrender.com/telnyx-media
STREAM_URL = os.getenv("STREAM_URL")  # Debe incluir el path /telnyx-media y usar wss://

if not STREAM_URL:
    raise ValueError("STREAM_URL debe estar configurado en .env con una URL pública wss://.../telnyx-media")
if not STREAM_URL.endswith("/telnyx-media"):
    raise ValueError("STREAM_URL debe terminar con '/telnyx-media' para coincidir con el endpoint WebSocket")

app = FastAPI()

def api(call_id: str, action: str, data: dict = None):
    try:
        resp = requests.post(
            f"https://api.telnyx.com/v2/calls/{call_id}/actions/{action}",
            headers={"Authorization": f"Bearer {TELNYX_API_KEY}"},
            json=data or {}
        )
        if not resp.ok:
            print(f"Error en comando '{action}' para call_id {call_id}: {resp.status_code} - {resp.text}")
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        print(f"Comando '{action}' enviado exitosamente: {resp.status_code} - {resp.text}")
        return resp
    except Exception as ex:
        print(f"Excepción al enviar comando '{action}': {str(ex)}")
        raise

@app.post("/telnyx-webhook")
async def telnyx_webhook(request: Request):
    data = await request.json()
    print("Webhook recibido completo:", json.dumps(data, indent=2))  # Log del payload completo para depuración
    e = data["data"]
    p = e["payload"]
    cid = p.get("call_control_id")
    direction = p.get("direction", "unknown")
    print(f"Evento recibido: {e['event_type']} - Dirección: {direction} - Call ID: {cid}")

    if e["event_type"] == "call.initiated":
        if direction == "incoming":  # Para inbound (incoming)
            print("Entrando a contestar llamada incoming...")
            try:
                api(cid, "answer")  # Contesta la llamada
            except Exception as ex:
                print(f"Error al contestar llamada: {str(ex)}")

    elif e["event_type"] == "call.answered":
        # Para ambos (inbound/outbound): Iniciar stream y reproducir mensaje (sin hangup para permitir grabación)
        try:
            print(f"Enviando streaming_start con URL: {STREAM_URL}")
            api(cid, "streaming_start", {
                "stream_url": STREAM_URL,
                "stream_track": "both_tracks"
            })
            print("Streaming start enviado; esperando conexión WebSocket...")
            api(cid, "speak", {
                "payload": "¡Hola! Llamada recibida exitosamente. Habla ahora para grabar.",
                "language": "es-MX",
                "voice": "female"
            })
            # No colgamos automáticamente para permitir que la llamada continúe y grabe audio
        except Exception as ex:
            print(f"Error al streaming_start/speak: {str(ex)}")
            # Intenta speak incluso si streaming falla (para pruebas)
            try:
                api(cid, "speak", {
                    "payload": "¡Hola! Llamada recibida exitosamente. Habla ahora para grabar.",
                    "language": "es-MX",
                    "voice": "female"
                })
            except:
                pass

    elif e["event_type"] == "call.hangup":
        # Opcional: Detener stream explícitamente si está activo
        try:
            api(cid, "streaming_stop")
        except Exception as ex:
            print(f"Error al streaming_stop: {str(ex)}")  # Podría fallar si no inició o ya terminó

    elif e["event_type"] == "streaming.failed":
        print(f"Streaming failed: Razón - {p.get('failure_reason')}, URL - {p.get('stream_params', {}).get('stream_url')} - Detalles completos: {json.dumps(p, indent=2)}")  # Log detalles extendidos para depuración

    else:
        print(f"Evento desconocido recibido: {e['event_type']} - Payload: {json.dumps(p, indent=2)}")  # Log para eventos no manejados

    return {"status": "OK"}

@app.post("/make_outbound_call")
async def make_outbound_call(request: Request):
    body = await request.json()
    to_number = body.get("to")
    if not to_number:
        raise HTTPException(status_code=400, detail="Falta 'to'")
    try:
        resp = requests.post(
            "https://api.telnyx.com/v2/calls",
            headers={"Authorization": f"Bearer {TELNYX_API_KEY}", "Content-Type": "application/json"},
            json={"connection_id": TELNYX_CONNECTION_ID, "to": to_number, "from": TELNYX_FROM_NUMBER}
        )
        if not resp.ok:
            print(f"Error al iniciar outbound: {resp.status_code} - {resp.text}")
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        return {"status": "Llamada outbound iniciada", "details": resp.json()}
    except Exception as ex:
        print(f"Excepción en outbound: {str(ex)}")
        raise

@app.get("/list-recordings")
async def list_recordings():
    directory = Path("llamadas")
    if not directory.exists() or not directory.is_dir():
        return {"message": "No hay grabaciones disponibles (directorio no creado o vacío)"}
    files = [f.name for f in directory.iterdir() if f.is_file()]
    if not files:
        return {"message": "No hay grabaciones disponibles"}
    print(f"Listando grabaciones: {files}")  # Log para depuración
    return {"files": files}

@app.get("/downloads/{filename}")
async def download_recording(filename: str):
    file_path = Path("llamadas") / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Archivo no encontrado")
    print(f"Descargando archivo: {filename}")  # Log para depuración
    return FileResponse(file_path, media_type="audio/wav", filename=filename)

@app.websocket("/telnyx-media")
async def telnyx_media(websocket: WebSocket):
    await websocket.accept()
    print("📶 WebSocket de Telnyx conectado")
    # Inicializamos variables para los archivos de audio (inbound y outbound)
    wave_files = {"inbound": None, "outbound": None}
    try:
        while True:
            # Recibir mensaje (Telnyx envía texto JSON)
            msg_text = await websocket.receive_text()
            data = json.loads(msg_text)
            event = data.get("event")
            print(f"Evento WebSocket recibido: {event} - Sequence: {data.get('sequence_number')} - Stream ID: {data.get('stream_id')}")  # Log extendido
            
            # Opcional: Imprimir JSON completo (descomentar si necesitas más detalles, pero payload es largo)
            # print("Full WS message:", json.dumps(data, indent=2))
            
            if event == "connected":
                print("Evento 'connected' recibido - Versión: {data.get('version')} - Conexión establecida correctamente.")
            
            elif event == "start":
                # Inicio del streaming: abrir archivos .wav para inbound y outbound
                call_session = data["start"].get("call_session_id", "call")
                timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                os.makedirs("llamadas", exist_ok=True)
                for track in ["inbound", "outbound"]:
                    filename = f"{call_session}_{timestamp}_{track}.wav"
                    file_path = os.path.join("llamadas", filename)
                    # Configurar archivo WAV (1 canal, 16-bit PCM, 8000 Hz)
                    wf = wave.open(file_path, "wb")
                    wf.setnchannels(1)
                    wf.setsampwidth(2)      # 16-bit = 2 bytes
                    wf.setframerate(8000)
                    wave_files[track] = wf
                    print(f"🎬 Iniciando grabación de {track} en {file_path}")
                    print(f"URL de descarga: https://testing-calls.onrender.com/downloads/{filename}")  # Log del link para descargar
            
            elif event == "media":
                # Telnyx nos envía un fragmento de audio
                media_data = data["media"]
                payload = media_data["payload"]  # audio base64
                track = media_data.get("track")  # "inbound" o "outbound"
                if track in wave_files and wave_files[track]:
                    try:
                        # Imprimir detalles del paquete para depuración
                        payload_preview = payload[:20] + "..." if len(payload) > 20 else payload
                        print(f"Paquete de media recibido para {track} - Largo payload base64: {len(payload)} - Preview: {payload_preview}")
                        
                        # Decodificar de base64 a bytes (u-law PCM)
                        audio_bytes = base64.b64decode(payload)
                        print(f"Decodificado u-law bytes: {len(audio_bytes)}")
                        
                        # Convertir de µ-law (PCMU) a PCM lineal 16-bit
                        pcm_bytes = audioop.ulaw2lin(audio_bytes, 2)  # 2 bytes = 16-bit
                        print(f"Convertido a PCM bytes: {len(pcm_bytes)}")
                        
                        # Escribir en el archivo WAV correspondiente
                        wave_files[track].writeframes(pcm_bytes)
                        print(f"Bytes escritos en archivo {track}: {len(pcm_bytes)}")
                    except base64.binascii.Error as be:
                        print(f"Error en decodificación base64 para {track}: {str(be)} - Payload inválido?")
                    except Exception as ae:
                        print(f"Error en conversión audio para {track}: {str(ae)}")
            
            elif event == "stop":
                # Fin del streaming de audio
                print("⏹ Finalizando stream de audio - Razón: {data.get('stop', {}).get('reason')}")
                break
            
            else:
                print(f"Evento WebSocket desconocido: {event} - Datos: {json.dumps(data)}")  # Log para eventos inesperados

    except WebSocketDisconnect as wsd:
        print(f"WebSocket desconectado: Código {wsd.code}, Razón {wsd.reason}")
    except Exception as ex:
        print(f"Error en WebSocket: {ex}")
    finally:
        # Cerrar archivos WAV y WebSocket
        for track, wf in wave_files.items():
            if wf:
                wf.close()
                file_path = wf.getname()  # Obtiene el path del archivo
                file_size = os.path.getsize(file_path)  # Tamaño en bytes
                status = "con audio" if file_size > 0 else "vacío - ¡posible problema!"
                print(f"Archivo {track} cerrado: {file_path} - Tamaño: {file_size} bytes ({status})")  # Confirmación extendida
                print(f"URL de descarga: https://testing-calls.onrender.com/downloads/{os.path.basename(file_path)}")  # Log del link
        await websocket.close()
        print("Conexión WebSocket cerrada")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))  # Usa $PORT en Render, fallback a 5000 l ocal
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False, ws="wsproto")