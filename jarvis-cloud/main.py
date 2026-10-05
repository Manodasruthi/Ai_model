import asyncio
from collections import deque
from contextlib import asynccontextmanager, suppress
import json
from pathlib import Path
import re
import secrets
import time
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from config import Settings
from providers import Providers, SYSTEM

ROOT = Path(__file__).resolve().parent
MAX_SECONDS = 20


class WindowLimit:
    def __init__(self, maximum: int, seconds: int = 60):
        self.maximum, self.seconds, self.events = maximum, seconds, deque()

    def take(self) -> bool:
        now = time.monotonic()
        while self.events and self.events[0] <= now - self.seconds:
            self.events.popleft()
        if len(self.events) >= self.maximum:
            return False
        self.events.append(now)
        return True


@asynccontextmanager
async def lifespan(app):
    settings = Settings.from_env()  # Fail closed if any secret is missing.
    async with httpx.AsyncClient(timeout=httpx.Timeout(35, connect=10),
                                 limits=httpx.Limits(max_connections=8)) as client:
        app.state.settings = settings
        app.state.providers = Providers(settings, client)
        app.state.auth_limit = WindowLimit(30)
        app.state.turn_limit = WindowLimit(8)
        app.state.active = False
        yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Permissions-Policy"] = "microphone=(self)"
    # Allow HF's wrapper to embed the app. Open direct app URL for mic problems.
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "connect-src 'self' wss:; media-src 'self' blob:; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'self' https://huggingface.co")
    return response


@app.get("/")
async def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/healthz")
async def health():
    return {"status": "ok"}  # Liveness, not a paid provider probe.


async def run_turn(ws, send, providers, history, *, pcm=None, rate=24000,
                   language="auto", reply_mode="auto", text=None):
    started = time.monotonic()
    queue = asyncio.Queue(maxsize=4)

    async def tts_worker():
        while True:
            sentence = await queue.get()
            if sentence is None:
                return
            await send({"type": "speaking"})
            async for chunk in providers.speak(sentence):
                await send(chunk)

    async def produce(messages):
        pending = ""
        total = 0
        async for token in providers.reply(messages):
            total += len(token)
            if total > 2400:
                raise ValueError("Reply too long")
            await send({"type": "token", "text": token})
            pending += token
            while True:
                boundary = re.search(r"[.!?\n](?:\s|$)", pending)
                cut = boundary.end() if boundary else 0
                if not cut and len(pending) > 240:
                    cut = pending.rfind(" ", 0, 240) + 1 or 240
                if not cut:
                    break
                sentence, pending = pending[:cut].strip(), pending[cut:]
                if sentence:
                    await queue.put(sentence)
        if pending.strip():
            await queue.put(pending.strip())
        await queue.put(None)

    try:
        async with asyncio.timeout(90):
            await send({"type": "thinking"})
            if text is None:
                text = await providers.transcribe(pcm, rate, language)
            if not text.strip():
                await send({"type": "error", "message": "No speech detected. Please try again."})
                return
            await send({"type": "transcript", "text": text})
            mode = {"auto": "Match the user's language.", "ta": "Reply in spoken Tamil.",
                    "en": "Reply in English.",
                    "tanglish": "Reply in conversational Tamil mixed with English technical words."}[reply_mode]
            messages = [{"role": "system", "content": SYSTEM + "\nReply mode: " + mode}]
            for turn in history:
                messages.extend(turn)
            first_new_message = len(messages)
            messages.append({"role": "user", "content": text})
            await send({"type": "audio_format", "sample_rate": 24000, "encoding": "pcm_s16le"})
            # A failed producer or consumer cancels its sibling, avoiding deadlocks.
            async with asyncio.TaskGroup() as group:
                group.create_task(tts_worker())
                group.create_task(produce(messages))
            history.append(messages[first_new_message:])
            # Keep complete tool-call/result groups together; cap context by characters.
            while len(history) > 3 or (len(history) > 1 and
                                       len(json.dumps(history, ensure_ascii=False)) > 6000):
                history.pop(0)
            await send({"type": "done", "elapsed_ms": round((time.monotonic() - started) * 1000)})
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        def leaves(error):
            if isinstance(error, BaseExceptionGroup):
                return [leaf for child in error.exceptions for leaf in leaves(child)]
            return [error]
        statuses = {e.response.status_code for e in leaves(exc) if isinstance(e, httpx.HTTPStatusError)}
        message = ("Provider quota reached. Wait and check your account limits."
                   if 429 in statuses else
                   "Provider authentication failed. Check the server API keys and region."
                   if statuses & {401, 403} else
                   "The request failed or timed out. Check model access, voice settings and provider status.")
        # Never return provider bodies/headers or credentials to the browser.
        await send({"type": "error", "message": message})


@app.websocket("/ws")
async def websocket(ws: WebSocket):
    state = app.state
    if ws.headers.get("origin") not in state.settings.origins or not state.auth_limit.take():
        await ws.close(code=1008)
        return
    await ws.accept()
    owned = False
    task = None
    send_lock = asyncio.Lock()

    async def send(value):
        async with send_lock:
            if isinstance(value, bytes):
                await ws.send_bytes(value)
            else:
                await ws.send_json(value)

    async def cancel():
        nonlocal task
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
                await task
            task = None

    try:
        first = await asyncio.wait_for(ws.receive_text(), timeout=8)
        if len(first) > 4096:
            raise ValueError("Invalid auth frame")
        auth = json.loads(first)
        key = auth.get("key", "") if isinstance(auth, dict) else ""
        if (not isinstance(key, str) or not secrets.compare_digest(key.encode(), state.settings.access_key.encode())
                or auth.get("type") != "auth"):
            await ws.close(code=1008, reason="Access denied")
            return
        if state.active:
            await ws.close(code=1013, reason="Another session is active")
            return
        state.active = owned = True
        await send({"type": "ready", "max_seconds": MAX_SECONDS})
        pcm = None
        history = []
        rate, language, reply_mode = 24000, "auto", "auto"
        opened = time.monotonic()
        recording_started = 0
        while True:
            remaining = 3600 - (time.monotonic() - opened)
            if remaining <= 0:
                break
            message = await asyncio.wait_for(ws.receive(), timeout=min(remaining, 180))
            if message["type"] == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                chunk = message["bytes"]
                if pcm is None or len(chunk) > 16384 or len(chunk) % 2:
                    raise ValueError("Invalid PCM frame")
                if len(pcm) + len(chunk) > rate * 2 * MAX_SECONDS or time.monotonic() - recording_started > 25:
                    raise ValueError("Recording limit exceeded")
                pcm.extend(chunk)
                continue
            raw = message.get("text", "")
            if len(raw) > 4096:
                raise ValueError("Control frame too large")
            control = json.loads(raw)
            if not isinstance(control, dict):
                raise ValueError("Invalid control frame")
            kind = control.get("type")
            if kind in {"cancel", "reset"}:
                await cancel()
                pcm = None
                if kind == "reset":
                    history.clear()
                await send({"type": "cancelled"})
                continue
            if task and not task.done():
                raise ValueError("Turn still running")
            if kind in {"start", "text"}:
                if pcm is not None:
                    raise ValueError("Already recording")
                language = control.get("language", "auto")
                reply_mode = control.get("reply_mode", "auto")
                if language not in {"auto", "ta", "en"} or reply_mode not in {"auto", "ta", "en", "tanglish"}:
                    raise ValueError("Invalid language")
                if not state.turn_limit.take():
                    await send({"type": "error", "message": "App limit: eight turns per minute. Please wait."})
                    continue
                if kind == "start":
                    rate = control.get("sample_rate")
                    if type(rate) is not int or rate not in {16000, 22050, 24000, 32000, 44100, 48000}:
                        raise ValueError("Unsupported sample rate")
                    pcm = bytearray()
                    recording_started = time.monotonic()
                    await send({"type": "recording"})
                else:
                    text = control.get("text", "")
                    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 1000:
                        raise ValueError("Invalid text")
                    task = asyncio.create_task(run_turn(ws, send, state.providers, history,
                                                       text=text.strip(), reply_mode=reply_mode))
            elif kind == "stop":
                if pcm is None:
                    raise ValueError("Not recording")
                data, pcm = bytes(pcm), None
                if len(data) < rate:  # At least half a second.
                    await send({"type": "error", "message": "Recording too short. Speak for at least half a second."})
                    continue
                task = asyncio.create_task(run_turn(ws, send, state.providers, history,
                                                   pcm=data, rate=rate, language=language,
                                                   reply_mode=reply_mode))
            else:
                raise ValueError("Unknown control message")
    except (WebSocketDisconnect, RuntimeError):
        pass
    except (ValueError, TypeError, KeyError, asyncio.TimeoutError):
        with suppress(Exception):
            await ws.close(code=1008, reason="Invalid request or session expired")
    finally:
        await cancel()
        if owned:
            state.active = False
        with suppress(Exception):
            await ws.close()
