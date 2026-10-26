#!/usr/bin/env python3
"""Model-resident Kokoro generator for the Speak Selection worker.

Run under the `mlx-audio` venv's python (see ``mdspeak.engine.MLX_AUDIO_PY``),
which is the only venv that can import ``mlx_audio``. This process loads the
Kokoro model exactly once and then renders text to WAV on request, which is what
makes a hotkey feature usable: a one-shot ``mlx_audio.tts.generate`` call pays a
~5 s model load per selection, so the long-lived worker spawns *this* instead
and holds it warm.

Protocol: one JSON object per line on stdin, one per line on stdout.

Request:  {"id": "abc", "text": "...", "out": "/abs/out.wav", "voice": "bm_lewis", "speed": 1.0}
Response: {"id": "abc", "ok": true, "path": "/abs/out.wav", "seconds": 1.2}
          {"id": "abc", "ok": false, "error": "..."}

The first line written is a banner: {"event": "ready", "model": "..."}.

``text`` is spoken as-is — no markdown normalization here. The MVP speaks the
selection verbatim; ``mdspeak.normalize`` can be wired in later without touching
this file.

This mirrors the read-aloud sidecar's ``synthesize.py --serve`` but lives in this
repo and drops the normalizer, since there is no markdown to strip.
"""

from __future__ import annotations

import json
import os
import sys
import wave

# mlx-audio prints progress to stdout, but this script's contract is "stdout is
# JSON, one object per line". Redirect the library's chatter to stderr and keep
# the real stdout for our own emit() calls.
_real_stdout = sys.stdout
sys.stdout = sys.stderr

from mlx_audio.tts.generate import generate_audio  # noqa: E402
from mlx_audio.tts.utils import load_model  # noqa: E402

DEFAULT_MODEL = os.environ.get("MDSPEAK_KOKORO_MODEL", "mlx-community/Kokoro-82M-8bit")
DEFAULT_VOICE = "bm_fable"


def lang_code_for(voice: str) -> str:
    """Kokoro picks its phonemizer from a one-letter language code carried by
    the voice name (a=American, b=British, ...). Deriving it keeps a British
    voice from being fed American phonemes."""
    return voice[0] if voice and voice[0] in "abefhijpz" else "a"


def wav_seconds(path: str) -> float:
    with wave.open(path, "rb") as w:
        return w.getnframes() / float(w.getframerate())


def emit(payload: dict) -> None:
    print(json.dumps(payload), file=_real_stdout, flush=True)


def synthesize(model, *, text: str, out: str, voice: str, speed: float) -> dict:
    raw = (text or "").strip()
    if not raw:
        return {"ok": False, "error": "empty text"}

    out_dir = os.path.dirname(os.path.abspath(out)) or "."
    os.makedirs(out_dir, exist_ok=True)
    # generate_audio names its output `<file_prefix>.<audio_format>`, so pass it
    # the prefix and let it append the extension.
    prefix = os.path.splitext(os.path.basename(out))[0]

    generate_audio(
        text=raw,
        model=model,
        voice=voice,
        speed=speed,
        lang_code=lang_code_for(voice),
        output_path=out_dir,
        file_prefix=prefix,
        audio_format="wav",
        join_audio=True,  # long text is chunked internally; we want one file
        play=False,
        verbose=False,
    )

    produced = os.path.join(out_dir, f"{prefix}.wav")
    if not os.path.exists(produced):
        return {"ok": False, "error": "synthesis produced no file"}
    return {"ok": True, "path": produced, "seconds": round(wav_seconds(produced), 2)}


def serve() -> int:
    model = load_model(model_path=DEFAULT_MODEL)
    emit({"event": "ready", "model": DEFAULT_MODEL})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as err:
            emit({"ok": False, "error": f"bad request json: {err}"})
            continue

        req_id = req.get("id")
        try:
            body = synthesize(
                model,
                text=req.get("text", ""),
                out=req["out"],
                voice=req.get("voice") or DEFAULT_VOICE,
                speed=float(req.get("speed", 1.0)),
            )
        except KeyError as err:
            body = {"ok": False, "error": f"missing field {err}"}
        except Exception as err:  # noqa: BLE001 — the loop must outlive one bad request
            body = {"ok": False, "error": f"{type(err).__name__}: {err}"}
        emit({"id": req_id, **body})

    return 0


if __name__ == "__main__":
    raise SystemExit(serve())
