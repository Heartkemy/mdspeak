"""Speak Selection worker — the warm, headless half of the menu-bar app.

The Swift shell (``native/SpeakSelection``) POSTs selected text here and plays
the WAV it gets back. This process is the long-lived HTTP worker the spec calls
for: it stays up for the shell's whole lifetime and holds the Kokoro model warm.

Two processes, one repo:

- **This** FastAPI server, run by the ``mdspeak`` venv (which has HTTP deps).
- ``kokoro_serve.py``, run by the ``mlx-audio`` venv python (which has the model
  but no HTTP stack). It loads Kokoro once and renders text to WAV over
  stdin/stdout JSON.

Splitting them is the same shape as the read-aloud sidecar (Node HTTP +
``synthesize.py --serve``): ``engine.synth_chunk`` shells out per call and pays a
~5 s model load every selection, which is exactly what would make a hotkey feel
dead. Instead the model loads once here, and a short selection renders
near-instantly.

Speech is the selection **as prose**: markdown is normalized via
``mdspeak.normalize`` before it reaches the model, so syntax is never voiced.
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

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from mdspeak import engine
from mdspeak.normalize import normalize

VOICE = "bm_lewis"
SERVE_SCRIPT = Path(__file__).parent / "kokoro_serve.py"

# Mutable default voice: `--voice` on the CLI overrides it; an empty voice on a
# request means "use the default". Kept separate from VOICE so the module-level
# constant (used as the fallback in kokoro_serve and elsewhere) never mutates.
_default_voice = VOICE

app = FastAPI(title="mdspeak-speak")


class SpeakIn(BaseModel):
    text: str
    voice: str = ""
    calm: bool = False  # heavier pauses (CALM_PAUSE) for dense text


@dataclass
class SpeakJob:
    id: str
    total: int
    done: int = 0
    error: str = ""
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
        proc = subprocess.Popen(
            [str(engine.MLX_AUDIO_PY), str(SERVE_SCRIPT)],
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
    with _lock:
        if _proc is not None and _proc.poll() is None:
            return
        _ready.clear()
        _proc_ready = False
        _proc_error = ""
        _spawn()


@app.get("/health")
def health() -> dict:
    alive = _proc is not None and _proc.poll() is None
    return {"ok": alive and _proc_ready, "ready": _proc_ready, "error": _proc_error}


@app.get("/voices")
def voices() -> dict:
    return {"voices": [{"id": v, "label": lbl} for v, lbl in engine.KOKORO_VOICES]}


@app.post("/speak")
def speak(body: SpeakIn) -> dict:
    """Queue a selection for speech. Rendered segment-by-segment so the shell
    can start playing the first chunk while the rest is still synthesizing."""
    text = normalize(body.text)
    if not text.strip():
        raise HTTPException(400, "nothing to speak")

    chunks = engine.chunk(text)
    job = SpeakJob(id=uuid.uuid4().hex[:12], total=len(chunks))
    JOBS[job.id] = job
    threading.Thread(target=_render, args=(job, chunks, body), daemon=True).start()
    return {"id": job.id, "segments": job.total}


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
        job.cond.wait_for(lambda: path.exists() or bool(job.error), timeout=600)
    if job.error:
        raise HTTPException(500, job.error)
    if not path.exists():
        raise HTTPException(404, "segment not ready")
    return Response(content=path.read_bytes(), media_type="audio/wav")


def _render(job: SpeakJob, chunks: list[str], body: SpeakIn) -> None:
    """Render each chunk to its own WAV, in order, marking progress as it goes."""
    out_dir = JOBS_DIR / job.id
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        _ensure_proc()
        if not _ready.wait(timeout=600) or not _proc_ready:
            raise RuntimeError(_proc_error or "generator not ready")
        for i, piece in enumerate(chunks):
            dest = out_dir / f"{i:04d}.wav"
            _render_piece(piece, dest, body.voice, body.calm)
            with job.cond:
                job.done = i + 1
                job.cond.notify_all()
    except Exception as exc:  # surfaced to the shell via the job, not swallowed
        job.error = f"{type(exc).__name__}: {exc}"
        with job.cond:
            job.cond.notify_all()


def _render_piece(text: str, dest: Path, voice: str, calm: bool) -> None:
    """One round-trip through the warm generator: text in, WAV out, to `dest`."""
    with _lock:
        with tempfile.TemporaryDirectory() as td:
            req_id = uuid.uuid4().hex[:12]
            out = Path(td) / "out.wav"
            _proc.stdin.write(
                json.dumps(
                    {"id": req_id, "text": text, "out": str(out), "voice": voice or _default_voice}
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
    parser = argparse.ArgumentParser(description="Speak Selection warm worker")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--voice", default=VOICE)
    args = parser.parse_args()

    global _default_voice
    _default_voice = args.voice

    # Warm the model in the background so /health reports honestly and the first
    # hotkey press doesn't pay the load.
    threading.Thread(target=_spawn, daemon=True).start()

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
