import asyncio
import base64
import json
import os
import pyaudio
from websockets.client import connect
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

class SimpleGeminiVoice:
    def __init__(self):
        self.audio_queue = asyncio.Queue()
        self.api_key = os.environ.get("GEMINI_API_KEY")
        self.model = "gemini-2.5-flash-preview-native-audio-dialog"
        self.uri = f"wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key={self.api_key}"
        # Audio settings
        self.FORMAT = pyaudio.paInt16
        self.CHANNELS = 1
        self.CHUNK = 512
        self.RATE = 16000
        self.model_speaking = False
        # Configuraciones personalizables
        self.system_prompt = "Eres chileno y buena onda."
        self.voice_name = "Puck"

    async def start(self):
        # Initialize websocket
        self.ws = await connect(
            self.uri,
            extra_headers={"Content-Type": "application/json"}
        )
        # Cambio: realtimeInputConfig como top-level en setup, no bajo generationConfig
        await self.ws.send(json.dumps({
            "setup": {
                "model": f"models/{self.model}",
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "speechConfig": {
                        "voiceConfig": {
                            "prebuiltVoiceConfig": {
                                "voiceName": self.voice_name
                            }
                        }
                    }
                },
                "systemInstruction": {
                    "parts": [{"text": self.system_prompt}]
                },
                "realtimeInputConfig": { # Top-level para VAD nativo
                    "automaticActivityDetection": {
                        "disabled": False,
                        "startOfSpeechSensitivity": "START_SENSITIVITY_UNSPECIFIED", # Baja para menos falsos positivos por echo
                        "endOfSpeechSensitivity": "END_SENSITIVITY_LOW",
                        "prefixPaddingMs": 20,
                        "silenceDurationMs": 200 # Silencio corto para turnos rápidos
                    }
                }
            }
        }))
        await self.ws.recv()
        print("Connected to Gemini, You can start talking now")
        # Start audio streaming
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self.capture_audio())
            tg.create_task(self.stream_audio())
            tg.create_task(self.play_response())

    async def capture_audio(self):
        audio = pyaudio.PyAudio()
        stream = audio.open(
            format=self.FORMAT,
            channels=self.CHANNELS,
            rate=self.RATE,
            input=True,
            frames_per_buffer=self.CHUNK,
        )
        while True:
            data = await asyncio.to_thread(stream.read, self.CHUNK)
            # Envío continuo de audio para barge-in (sin condición de model_speaking)
            await self.ws.send(json.dumps({
                "realtime_input": {
                    "media_chunks": [{
                        "data": base64.b64encode(data).decode(),
                        "mime_type": "audio/pcm;rate=16000",
                    }]
                }
            }))

    async def stream_audio(self):
        async for msg in self.ws:
            response = json.loads(msg)
            try:
                audio_data = response["serverContent"]["modelTurn"]["parts"][0]["inlineData"]["data"]
                if not self.model_speaking:
                    self.model_speaking = True
                    print("\nModel started speaking")
                self.audio_queue.put_nowait(base64.b64decode(audio_data))
            except KeyError:
                pass
            
            # Manejo de interrupciones (barge-in)
            try:
                interrupted = response["serverContent"]["interrupted"]
                if interrupted:
                    print("\nInterrupted by user")
                    # Limpia queue y detiene playback
                    while not self.audio_queue.empty():
                        self.audio_queue.get_nowait()
                    self.model_speaking = False
            except KeyError:
                pass
            try:
                turn_complete = response["serverContent"]["turnComplete"]
            except KeyError:
                pass
            else:
                if turn_complete:
                    print("\nEnd of turn")
                    await asyncio.sleep(0.1) # Pausa mínima para menos latencia
                    while not self.audio_queue.empty():
                        self.audio_queue.get_nowait()
                    self.model_speaking = False
                    print("Ready for next input")

    async def play_response(self):
        audio = pyaudio.PyAudio()
        stream = audio.open(
            format=self.FORMAT,
            channels=self.CHANNELS,
            rate=24000,
            output=True
        )
        while True:
            data = await self.audio_queue.get()
            await asyncio.to_thread(stream.write, data)

if __name__ == "__main__":
    client = SimpleGeminiVoice()
    asyncio.run(client.start())