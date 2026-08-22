"""mdspeak — paste markdown, hear it read back. Local MLX models only.

One process: a small HTTP API plus one static page. Synthesis runs on a worker
thread per job so the page can poll progress; there is no queue and no database
because there is one user and one job at a time.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from mdspeak import engine
from mdspeak.normalize import leaked_syntax, normalize

WEB = Path(__file__).parent / "web"
JOBS_DIR = Path.home() / ".cache/mdspeak/jobs"

app = FastAPI(title="mdspeak")


@dataclass
class Job:
    id: str
    total: int
    done: int = 0
    state: str = "running"  # running | ready | error | cancelled
    error: str = ""
    duration: float = 0.0
    text: str = ""
    cancel: threading.Event = field(default_factory=threading.Event)


JOBS: dict[str, Job] = {}


class SpeakIn(BaseModel):
    markdown: str
    engine: str = "kokoro"
    emotion: str = ""
    voice: str = ""
    read_code: bool = False


class PreviewIn(BaseModel):
    markdown: str
    read_code: bool = False


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (WEB / "index.html").read_text()


@app.get("/api/engines")
def api_engines() -> dict:
    return {
        "engines": [e.__dict__ for e in engine.engines()],
        "emotions": engine.INDEXTTS_EMOTIONS,
        "kokoroVoices": [{"id": v, "label": lbl} for v, lbl in engine.KOKORO_VOICES],
    }


@app.post("/api/preview")
def api_preview(body: PreviewIn) -> dict:
    """The exact string the engine will be fed. This is the A7 receipt."""
    text = normalize(body.markdown, read_code=body.read_code)
    return {
        "text": text,
        "leaks": leaked_syntax(text),
        "chunks": len(engine.chunk(text)),
        "chars": len(text),
    }


@app.post("/api/speak")
def api_speak(body: SpeakIn) -> dict:
    text = normalize(body.markdown, read_code=body.read_code)
    if not text.strip():
        raise HTTPException(400, "nothing to read")
    leaks = leaked_syntax(text)
    if leaks:
        # Fail loud. Voicing markdown syntax is the one outcome this app exists
        # to prevent, so it is a hard error, not a warning in a log.
        raise HTTPException(500, f"markdown syntax leaked into speech: {leaks}")

    # An engine that chunks internally gets the whole text in one call — one
    # model load instead of one per chunk. Its progress bar then has a single
    # step, which is the honest thing to show: there is one operation.
    chunks = [text] if engine.batches_for(body.engine) else engine.chunk(text)
    job = Job(id=uuid.uuid4().hex[:12], total=len(chunks), text=text)
    JOBS[job.id] = job
    threading.Thread(
        target=_work, args=(job, chunks, body), daemon=True
    ).start()
    return {"id": job.id, "chunks": job.total}


def _work(job: Job, chunks: list[str], body: SpeakIn) -> None:
    out_dir = JOBS_DIR / job.id
    out_dir.mkdir(parents=True, exist_ok=True)
    parts: list[Path] = []
    try:
        for i, piece in enumerate(chunks):
            if job.cancel.is_set():
                job.state = "cancelled"
                return
            part = out_dir / f"part_{i:04d}.wav"
            engine.synth_chunk(
                piece, part, body.engine, voice=body.voice, emotion=body.emotion
            )
            parts.append(part)
            job.done = i + 1
        final = out_dir / "audio.wav"
        engine.join_wavs(parts, final)
        job.duration = engine.wav_duration(final)
        job.state = "ready"
    except Exception as exc:  # surfaced to the page, not swallowed
        job.state = "error"
        job.error = str(exc)


@app.get("/api/job/{job_id}")
def api_job(job_id: str) -> dict:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    return {
        "id": job.id,
        "state": job.state,
        "done": job.done,
        "total": job.total,
        "duration": round(job.duration, 2),
        "error": job.error,
    }


@app.post("/api/job/{job_id}/cancel")
def api_cancel(job_id: str) -> dict:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    job.cancel.set()
    return {"ok": True}


@app.get("/api/audio/{job_id}.wav")
def api_audio(job_id: str) -> FileResponse:
    path = JOBS_DIR / job_id / "audio.wav"
    if not path.exists():
        raise HTTPException(404, "not ready")
    return FileResponse(path, media_type="audio/wav")
