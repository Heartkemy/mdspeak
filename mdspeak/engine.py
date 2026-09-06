"""TTS engines: thin subprocess wrappers around the two local MLX runtimes.

Neither runtime is importable from this process — they live in their own venvs
with conflicting torch/transformers pins. Shelling out to the CLIs that already
work is the whole integration; there is no third copy of the inference code.

Measured on this machine (Apple M5, 24 GB), warm, one paragraph of prose:

    kokoro     RTF 0.16  — 6.4x faster than real time, one call for the lot
    marvis     RTF 0.80  — faster than real time
    indextts2  RTF 6.17  — 24 s of speech costs 150 s

Kokoro is the default: it is the fastest by a wide margin and the same engine
the trading-analysis-system app uses, so the two agree. Marvis is kept as a
smaller fallback and IndexTTS-2 because it is the better voice and some jobs
are worth waiting for.
"""

from __future__ import annotations

import glob
import shutil
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

# Canonical pause-widening module (vendored into read-aloud as
# `python/breathe.py`). Re-exported so server.py and tests use one name.
from mdspeak.breathe import CALM_PAUSE, DEFAULT_PAUSE, breathe

MLX_AUDIO_PY = Path("/Users/rafaelhorvat/tools/mlx-audio/repo/.venv/bin/python")
INDEXTTS_BIN = Path("/Users/rafaelhorvat/__code__/mlx-indextts/.venv/bin/mlx-indextts")
MARVIS_REPO = "Marvis-AI/marvis-tts-100m-v0.2-MLX-6bit"
INDEXTTS_SNAPSHOT_GLOB = str(
    Path.home()
    / ".cache/huggingface/hub/models--mlx-community--IndexTTS-2-fp16/snapshots/*"
)
INDEXTTS_REF = Path("/Users/rafaelhorvat/__code__/osho-tts/ref4.wav")

KOKORO_REPO = "prince-canuma/Kokoro-82M"

MARVIS_VOICES = ["conversational_a", "conversational_b"]
INDEXTTS_EMOTIONS = [
    "calm", "happy", "sad", "angry", "afraid", "disgusted", "melancholic", "surprised",
]

# The English Kokoro voices worth offering for reading agent output aloud; the
# model ships 50+ including other languages. Same shortlist the trading app
# curated. `b*` are British, `a*` American — and that leading letter is also
# what picks the grapheme-to-phoneme front-end, see `_kokoro_lang`.
KOKORO_VOICES = [
    ("bm_fable", "Fable — British male, warm"),
    ("bm_lewis", "Lewis — British male, the voice chosen for agent output"),
    ("bm_george", "George — British male, low and steady"),
    ("bm_daniel", "Daniel — British male, soft-spoken"),
    ("am_michael", "Michael — American male, gentle"),
    ("am_onyx", "Onyx — American male, deep"),
    ("af_heart", "Heart — American female, warm"),
    ("af_nicole", "Nicole — American female, hushed"),
    ("bf_emma", "Emma — British female, calm"),
]
KOKORO_DEFAULT_VOICE = "bm_fable"


def _kokoro_lang(voice: str) -> str:
    """Kokoro picks its phonemizer from a one-letter language code, and voice
    names carry that letter first: a=American, b=British, e=Spanish, f=French,
    h=Hindi, i=Italian, j=Japanese, p=Portuguese, z=Mandarin. Deriving it from
    the voice stops a British voice being fed American phonemes, which is
    audible rather than cosmetic."""
    return voice[0] if voice and voice[0] in "abefhijpz" else "a"


@dataclass(frozen=True)
class Engine:
    key: str
    label: str
    note: str
    available: bool
    detail: str = ""
    # True when the runtime chunks long input itself, so the whole text can go
    # in one subprocess. Each subprocess reloads the model (~5 s measured for
    # Kokoro), so per-chunk calls would spend 40 s of pure load on an 8-chunk
    # document — more than the synthesis. Engines without this still go chunk by
    # chunk, which is also what keeps their progress bar meaningful.
    batches: bool = False
    # Offered in the engine dropdown. Backend-only engines (marvis, indextts2)
    # stay callable via /api/speak but are not suggested to the user.
    suggested: bool = True


def _has_misaki() -> bool:
    """Whether the mlx-audio venv can phonemize English (Kokoro's front-end)."""
    if not MLX_AUDIO_PY.exists():
        return False
    probe = subprocess.run(
        [str(MLX_AUDIO_PY), "-c", "import misaki.en"], capture_output=True, timeout=120
    )
    return probe.returncode == 0


def _indextts_snapshot() -> Path | None:
    hits = sorted(glob.glob(INDEXTTS_SNAPSHOT_GLOB))
    for h in hits:
        if (Path(h) / "gpt.safetensors").exists():
            return Path(h)
    return None


def engines() -> list[Engine]:
    """What can actually run right now — checked on disk, never assumed."""
    snap = _indextts_snapshot()
    return [
        Engine(
            key="kokoro",
            label="Kokoro 82M",
            note="fastest — about 6x faster than real time",
            # Needs misaki for grapheme-to-phoneme; without it mlx-audio fails
            # at generation time rather than at import, so check for it here.
            available=MLX_AUDIO_PY.exists() and _has_misaki(),
            detail=KOKORO_REPO,
            batches=True,
        ),
        Engine(
            key="marvis",
            label="Marvis 100M",
            note="fast — about real time",
            available=MLX_AUDIO_PY.exists(),
            detail=MARVIS_REPO,
            suggested=False,
        ),
        Engine(
            key="indextts2",
            label="IndexTTS-2",
            note="better voice — about 6x slower than real time",
            available=bool(snap) and INDEXTTS_BIN.exists() and INDEXTTS_REF.exists(),
            detail=str(snap or "not on disk"),
            suggested=False,
        ),
    ]


def batches_for(key: str) -> bool:
    """Whether `key` accepts the whole text in one call. See Engine.batches."""
    return any(e.key == key and e.batches for e in engines())


# ---------------------------------------------------------------- chunking

def chunk(text: str, limit: int = 350) -> list[str]:
    """Split prose into generation-sized pieces at sentence ends.

    Both models degrade on long inputs, and chunking is also what makes the
    progress bar mean something. Splitting on blank lines first keeps a heading
    from being glued to the paragraph under it.
    """
    out: list[str] = []
    for block in [b.strip() for b in text.split("\n") if b.strip()]:
        if len(block) <= limit:
            out.append(block)
            continue
        buf = ""
        for piece in _sentences(block):
            if buf and len(buf) + len(piece) + 1 > limit:
                out.append(buf.strip())
                buf = piece
            else:
                buf = f"{buf} {piece}".strip()
        if buf.strip():
            out.append(buf.strip())
    return out


def _sentences(block: str) -> list[str]:
    parts: list[str] = []
    buf = ""
    for ch in block:
        buf += ch
        if ch in ".!?" and len(buf) > 1:
            parts.append(buf)
            buf = ""
    if buf.strip():
        parts.append(buf)
    return parts or [block]


# ---------------------------------------------------------------- synthesis

class SynthError(RuntimeError):
    pass


def _run(cmd: list[str], cwd: Path | None = None) -> None:
    proc = subprocess.run(
        [str(c) for c in cmd],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=1800,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-4:]
        raise SynthError(" / ".join(tail) or f"exit {proc.returncode}")


def synth_chunk(text: str, dest: Path, engine: str, *, voice: str = "", emotion: str = "") -> Path:
    """Render one chunk to `dest` (a .wav path). Returns dest."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if engine == "kokoro":
        v = voice or KOKORO_DEFAULT_VOICE
        with tempfile.TemporaryDirectory() as td:
            _run(
                [
                    MLX_AUDIO_PY, "-m", "mlx_audio.tts.generate",
                    "--model", KOKORO_REPO,
                    "--text", text,
                    "--voice", v,
                    "--lang_code", _kokoro_lang(v),
                    "--output_path", td,
                    "--join_audio",
                    "--audio_format", "wav",
                ],
                cwd=MLX_AUDIO_PY.parent.parent.parent,
            )
            made = sorted(Path(td).glob("*.wav"))
            if not made:
                raise SynthError("engine produced no audio")
            shutil.move(str(made[0]), dest)
        return dest

    if engine == "marvis":
        with tempfile.TemporaryDirectory() as td:
            cmd = [
                MLX_AUDIO_PY, "-m", "mlx_audio.tts.generate",
                "--model", MARVIS_REPO,
                "--text", text,
                "--output_path", td,
                "--join_audio",
                "--audio_format", "wav",
            ]
            if voice:
                cmd += ["--voice", voice]
            _run(cmd, cwd=MLX_AUDIO_PY.parent.parent.parent)
            made = sorted(Path(td).glob("*.wav"))
            if not made:
                raise SynthError("engine produced no audio")
            shutil.move(str(made[0]), dest)
        return dest

    if engine == "indextts2":
        snap = _indextts_snapshot()
        if not snap:
            raise SynthError("IndexTTS-2 weights not found on disk")
        cmd = [
            INDEXTTS_BIN, "generate",
            "-m", snap,
            "-r", _indextts_speaker(snap),
            "-t", text,
            "-o", dest,
            "-q", "8",
        ]
        if emotion:
            cmd += ["--emotion", emotion, "--emo-alpha", "0.6"]
        _run(cmd)
        # The CLI writes into an output/ dir next to the target on some paths.
        if not dest.exists():
            stray = sorted(dest.parent.glob("**/audio_*.wav"))
            if not stray:
                raise SynthError("engine produced no audio")
            shutil.move(str(stray[0]), dest)
        return dest

    raise SynthError(f"unknown engine {engine!r}")


_SPEAKER_CACHE = Path.home() / ".cache/mdspeak/indextts2_speaker.npz"


def _indextts_speaker(snap: Path) -> Path:
    """Pre-computed speaker conditioning: 9 s of load time down to 1.5 s."""
    if _SPEAKER_CACHE.exists():
        return _SPEAKER_CACHE
    _SPEAKER_CACHE.parent.mkdir(parents=True, exist_ok=True)
    _run([INDEXTTS_BIN, "speaker", "-m", snap, "-r", INDEXTTS_REF, "-o", _SPEAKER_CACHE])
    return _SPEAKER_CACHE


# ---------------------------------------------------------------- joining

# Breathing room (DEFAULT_PAUSE / CALM_PAUSE / breathe) lives in
# `mdspeak.breathe` — see its docstring. Applied in server._work after joining.

def join_wavs(parts: list[Path], dest: Path) -> Path:
    """Concatenate same-format wavs. stdlib `wave` — no resampling needed."""
    parts = [p for p in parts if p.exists() and p.stat().st_size > 44]
    if not parts:
        raise SynthError("nothing to join")
    if len(parts) == 1:
        shutil.copy(parts[0], dest)
        return dest
    with wave.open(str(parts[0]), "rb") as first:
        params = first.getparams()
    with wave.open(str(dest), "wb") as out:
        out.setparams(params)
        for p in parts:
            with wave.open(str(p), "rb") as w:
                if w.getframerate() != params.framerate or w.getnchannels() != params.nchannels:
                    raise SynthError("chunk format mismatch")
                out.writeframes(w.readframes(w.getnframes()))
    return dest


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())
