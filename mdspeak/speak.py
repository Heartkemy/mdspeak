"""Headless speech worker for the native Speak Selection app.

Kokoro is the supported local default.  Separately configured cloud engines
remain optional per request; all paths normalize markdown and publish WAV
segments for incremental playback.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Literal

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel

from mdspeak import engine
from mdspeak.local import guard_local_request, require_text_size
from mdspeak.normalize import normalize

VOICE = engine.KOKORO_DEFAULT_VOICE
SERVE_SCRIPT = Path(__file__).parent / "kokoro_serve.py"

# Mutable default voice: `--voice` on the CLI overrides it; an empty voice on a
# request means "use the default". Kept separate from VOICE so the module-level
# constant (used as the fallback in kokoro_serve and elsewhere) never mutates.
_default_voice = VOICE
_default_engine = "kokoro"

app = FastAPI(title="mdspeak-speak")
EngineKey = Literal["cloudflare", "gemini", "kokoro"]


@app.middleware("http")
async def local_only(request: Request, call_next):
    return await guard_local_request(request, call_next)


class SpeakIn(BaseModel):
    text: str
    engine: EngineKey | None = None
    voice: str = ""
    calm: bool = False  # heavier pauses (CALM_PAUSE) for dense text


@dataclass
class SpeakJob:
    id: str
    total: int
    done: int = 0
    error: str = ""
    cancel: threading.Event = field(default_factory=threading.Event)
    cond: threading.Condition = field(default_factory=threading.Condition)


JOBS: dict[str, SpeakJob] = {}
JOBS_DIR = Path.home() / ".cache/mdspeak/speak-jobs"


_proc: subprocess.Popen | None = None
_proc_ready = False
_proc_error = ""
_ready = threading.Event()
_lock = threading.Lock()


def _spawn() -> None:
    """Start the model-resident generator and wait for its ready banner."""
    global _proc, _proc_ready, _proc_error
    try:
        runtime = engine.mlx_audio_python()
        if not runtime:
            raise RuntimeError(engine.mlx_audio_error(needs_python=True))
        proc = subprocess.Popen(
            [str(runtime), str(SERVE_SCRIPT)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,  # mlx-audio progress goes to the worker's own stderr
            text=True,
            bufsize=1,
        )
        _proc = proc
        line = proc.stdout.readline()
        if not line:
            _proc_error = "generator exited before ready"
            return
        banner = json.loads(line)
        if banner.get("event") != "ready":
            _proc_error = f"unexpected banner: {line.strip()}"
            return
        _proc_ready = True
        _ready.set()
        print("kokoro_serve ready", file=sys.stderr, flush=True)
    except Exception as exc:  # surfaced via /health, not swallowed
        _proc_error = f"{type(exc).__name__}: {exc}"


def _ensure_proc() -> None:
    """Idempotent: warm the generator, restarting it if it ever died."""
    global _proc_ready, _proc_error
    with _lock:
        if _proc is not None and _proc.poll() is None:
            return
        _ready.clear()
        _proc_ready = False
        _proc_error = ""
        _spawn()


@app.get("/health")
def health(engine_key: Annotated[EngineKey | None, Query(alias="engine")] = None) -> dict:
    provider = engine_key or _default_engine
    if provider == "cloudflare":
        try:
            engine.cloudflare_credentials()
            return {"ok": True, "ready": True, "error": ""}
        except engine.SynthError as exc:
            return {"ok": False, "ready": False, "error": str(exc)}
    if provider == "gemini":
        try:
            ready = bool(engine.gemini_api_key())
            error = "" if ready else "Set GEMINI_API_KEY in the project .env file"
        except engine.SynthError as exc:
            ready, error = False, str(exc)
        return {"ok": ready, "ready": ready, "error": error}
    if _proc is None and not engine.mlx_audio_python():
        return {"ok": False, "ready": False, "error": engine.mlx_audio_error(needs_python=True)}
    alive = _proc is not None and _proc.poll() is None
    return {"ok": alive and _proc_ready, "ready": _proc_ready, "error": _proc_error}


@app.get("/voices")
def voices(engine_key: Annotated[EngineKey | None, Query(alias="engine")] = None) -> dict:
    choices = {"cloudflare": engine.CLOUDFLARE_VOICES,
               "gemini": engine.GEMINI_VOICES, "kokoro": engine.KOKORO_VOICES}[engine_key or _default_engine]
    return {"voices": [{"id": v, "label": lbl} for v, lbl in choices]}


@app.post("/speak")
def speak(body: SpeakIn) -> dict:
    """Queue a selection for speech. Rendered segment-by-segment so the shell
    can start playing the first chunk while the rest is still synthesizing."""
    require_text_size(body.text)
    text = normalize(body.text)
    if not text.strip():
        raise HTTPException(400, "nothing to speak")
    # Capture the engine for this job; changing the UI must not reroute its chunks.
    body.engine = body.engine or _default_engine
    if body.engine == "kokoro":
        if not engine.mlx_audio_python():
            raise HTTPException(503, engine.mlx_audio_error(needs_python=True))
    else:
        status = health(body.engine)
        if not status["ready"]:
            raise HTTPException(503, status["error"])

    chunks = engine.chunk(text)
    job = SpeakJob(id=uuid.uuid4().hex[:12], total=len(chunks))
    JOBS[job.id] = job
    threading.Thread(target=_render, args=(job, chunks, body), daemon=True).start()
    return {"id": job.id, "segments": job.total}


@app.post("/cancel/{job_id}")
def cancel(job_id: str) -> dict:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    with job.cond:
        job.cancel.set()
        job.cond.notify_all()
    return {"ok": True}


@app.get("/segment/{job_id}/{index}.wav")
def segment(job_id: str, index: int) -> Response:
    """Return one rendered segment, blocking until it's ready (or failed)."""
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    if index < 0 or index >= job.total:
        raise HTTPException(404, "no such segment")
    path = JOBS_DIR / job_id / f"{index:04d}.wav"
    with job.cond:
        job.cond.wait_for(lambda: path.exists() or bool(job.error) or job.cancel.is_set(), timeout=600)
    if job.cancel.is_set():
        raise HTTPException(409, "speech cancelled")
    if job.error:
        raise HTTPException(500, job.error)
    if not path.exists():
        raise HTTPException(404, "segment not ready")
    return Response(content=path.read_bytes(), media_type="audio/wav")


def _render(job: SpeakJob, chunks: list[str], body: SpeakIn) -> None:
    """Render each chunk to its own WAV, in order, marking progress as it goes."""
    out_dir = JOBS_DIR / job.id
    out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    provider = body.engine or _default_engine
    try:
        if provider == "kokoro":
            _ensure_proc()
            if not _ready.wait(timeout=600) or not _proc_ready:
                raise RuntimeError(_proc_error or "generator not ready")
        for i, piece in enumerate(chunks):
            if job.cancel.is_set():
                return
            dest = out_dir / f"{i:04d}.wav"
            _render_piece(piece, dest, body.voice, body.calm, provider)
            with job.cond:
                job.done = i + 1
                job.cond.notify_all()
    except Exception as exc:  # surfaced to the shell via the job, not swallowed
        job.error = f"{type(exc).__name__}: {exc}"
        with job.cond:
            job.cond.notify_all()


def _render_piece(text: str, dest: Path, voice: str, calm: bool, provider: str) -> None:
    """Publish a segment only after synthesis and pause processing are complete."""
    default_voice = _default_voice if provider == _default_engine else ""
    if provider in {"cloudflare", "gemini"}:
        # Older app installs can still send a saved voice for another engine.
        legacy_voices = engine.KOKORO_VOICES + (
            engine.GEMINI_VOICES if provider == "cloudflare" else engine.CLOUDFLARE_VOICES
        )
        voice = voice or default_voice
        if voice in {v for v, _ in legacy_voices}:
            voice = ""
        with tempfile.TemporaryDirectory(dir=dest.parent) as td:
            wav = engine.synth_chunk(text, Path(td) / "out.wav", provider, voice=voice)
            engine.breathe(wav, engine.CALM_PAUSE if calm else engine.DEFAULT_PAUSE)
            wav.replace(dest)
        return
    with _lock:
        with tempfile.TemporaryDirectory() as td:
            req_id = uuid.uuid4().hex[:12]
            out = Path(td) / "out.wav"
            _proc.stdin.write(
                json.dumps(
                    {"id": req_id, "text": text, "out": str(out), "voice": voice or default_voice or VOICE}
                )
                + "\n"
            )
            _proc.stdin.flush()
            line = _proc.stdout.readline()
            if not line:
                raise RuntimeError("generator died mid-request")
            resp = json.loads(line)
            if not resp.get("ok"):
                raise RuntimeError(resp.get("error", "synthesis failed"))
            wav = Path(resp["path"])
            # Breathing room: widen the natural pauses already in the audio, the
            # same pass the web player applies. Calm asks for a heavier gap.
            engine.breathe(wav, engine.CALM_PAUSE if calm else engine.DEFAULT_PAUSE)
            shutil.move(str(wav), dest)


def main() -> None:
    parser = argparse.ArgumentParser(description="Speak Selection speech worker")
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--engine", choices=["cloudflare", "gemini", "kokoro"], default="kokoro")
    parser.add_argument("--voice", default=VOICE)
    args = parser.parse_args()

    global _default_voice, _default_engine
    _default_voice = args.voice
    _default_engine = args.engine

    # Warm the model in the background so /health reports honestly and the first
    # hotkey press doesn't pay the load.
    if _default_engine == "kokoro":
        threading.Thread(target=_ensure_proc, daemon=True).start()

    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
