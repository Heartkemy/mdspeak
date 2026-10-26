"""TTS engines discovered from a user's local MLX-Audio installation.

Kokoro is the supported local baseline.  The application deliberately does not
bundle a Python interpreter, model weights, or a reference voice: those belong
to the user's separately installed runtime and remain governed by their
upstream terms.
"""

from __future__ import annotations

import glob
import os
import shlex
import shutil
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

# Canonical pause-widening module (vendored into read-aloud as
# `python/breathe.py`). Re-exported so server.py and tests use one name.
from mdspeak.breathe import CALM_PAUSE, DEFAULT_PAUSE, breathe

MARVIS_REPO = "Marvis-AI/marvis-tts-100m-v0.2-MLX-6bit"
INDEXTTS_SNAPSHOT_GLOB = str(
    Path.home()
    / ".cache/huggingface/hub/models--mlx-community--IndexTTS-2-fp16/snapshots/*"
)
KOKORO_REPO = os.environ.get("MDSPEAK_KOKORO_MODEL", "mlx-community/Kokoro-82M-8bit")

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


def _resolve_executable(value: str) -> Path | None:
    """Resolve an explicit path or a command name without guessing locations."""
    candidate = Path(value).expanduser()
    if candidate.is_absolute() or "/" in value:
        return candidate if candidate.is_file() and os.access(candidate, os.X_OK) else None
    found = shutil.which(value)
    return Path(found) if found else None


def mlx_audio_command() -> Path | None:
    """The MLX-Audio CLI from explicit configuration or the user's PATH."""
    configured = os.environ.get("MDSPEAK_MLX_AUDIO_COMMAND", "").strip()
    return _resolve_executable(configured) if configured else _resolve_executable("mlx_audio.tts.generate")


def mlx_audio_python() -> Path | None:
    """Find the interpreter behind the installed CLI for the warm native worker."""
    configured = os.environ.get("MDSPEAK_MLX_AUDIO_PYTHON", "").strip()
    if configured:
        return _resolve_executable(configured)

    command = mlx_audio_command()
    if not command:
        return None
    try:
        first = command.read_text(encoding="utf-8").splitlines()[0]
    except (OSError, UnicodeDecodeError, IndexError):
        return None
    if not first.startswith("#!"):
        return None
    parts = shlex.split(first[2:].strip())
    if not parts:
        return None
    executable = parts[0]
    if Path(executable).name == "env":
        executable = parts[1] if len(parts) > 1 else ""
    return _resolve_executable(executable) if executable else None


def mlx_audio_error(*, needs_python: bool = False) -> str:
    if needs_python:
        return (
            "Kokoro's warm worker needs the mlx-audio interpreter. "
            "Set MDSPEAK_MLX_AUDIO_PYTHON to that virtual environment's Python."
        )
    return (
        "Kokoro needs mlx-audio. Install it with "
        "'uv tool install --force mlx-audio --prerelease=allow', ensure "
        "'mlx_audio.tts.generate' is on PATH, or set MDSPEAK_MLX_AUDIO_COMMAND."
    )


def _indextts_enabled() -> bool:
    return os.environ.get("MDSPEAK_ENABLE_UNSUPPORTED_INDEXTTS") == "1"


def _indextts_bin() -> Path | None:
    configured = os.environ.get("MDSPEAK_INDEXTTS_COMMAND", "").strip()
    return _resolve_executable(configured) if configured else None


def _indextts_reference() -> Path | None:
    configured = os.environ.get("MDSPEAK_INDEXTTS_REFERENCE_AUDIO", "").strip()
    if not configured:
        return None
    path = Path(configured).expanduser()
    return path if path.is_file() else None


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


def _indextts_snapshot() -> Path | None:
    hits = sorted(glob.glob(INDEXTTS_SNAPSHOT_GLOB))
    for h in hits:
        if (Path(h) / "gpt.safetensors").exists():
            return Path(h)
    return None


def engines() -> list[Engine]:
    """Configured engines, without loading a model or downloading any weights."""
    snap = _indextts_snapshot()
    choices = [
        Engine(
            key="kokoro",
            label="Kokoro 82M",
            note="supported local default",
            available=mlx_audio_command() is not None,
            detail=KOKORO_REPO,
            batches=True,
        ),
        Engine(
            key="marvis",
            label="Marvis 100M",
            note="experimental alternative",
            available=mlx_audio_command() is not None,
            detail=MARVIS_REPO,
            suggested=False,
        ),
    ]
    if _indextts_enabled():
        choices.append(
            Engine(
                key="indextts2",
                label="IndexTTS-2",
                note="unsupported custom-model path",
                available=bool(snap) and _indextts_bin() is not None and _indextts_reference() is not None,
                detail=str(snap or "not on disk"),
                suggested=False,
            )
        )
    return choices


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


def _mlx_audio(args: list[str], *, cwd: Path | None = None) -> None:
    command = mlx_audio_command()
    if not command:
        raise SynthError(mlx_audio_error())
    _run([str(command), *args], cwd=cwd)


def synth_chunk(text: str, dest: Path, engine: str, *, voice: str = "", emotion: str = "") -> Path:
    """Render one chunk to `dest` (a .wav path). Returns dest."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if engine == "kokoro":
        v = voice or KOKORO_DEFAULT_VOICE
        with tempfile.TemporaryDirectory() as td:
            _mlx_audio(
                [
                    "--model", KOKORO_REPO,
                    "--text", text,
                    "--voice", v,
                    "--lang_code", _kokoro_lang(v),
                    "--output_path", td,
                    "--join_audio",
                    "--audio_format", "wav",
                ]
            )
            made = sorted(Path(td).glob("*.wav"))
            if not made:
                raise SynthError("engine produced no audio")
            shutil.move(str(made[0]), dest)
        return dest

    if engine == "marvis":
        with tempfile.TemporaryDirectory() as td:
            cmd = [
                "--model", MARVIS_REPO,
                "--text", text,
                "--output_path", td,
                "--join_audio",
                "--audio_format", "wav",
            ]
            if voice:
                cmd += ["--voice", voice]
            _mlx_audio(cmd)
            made = sorted(Path(td).glob("*.wav"))
            if not made:
                raise SynthError("engine produced no audio")
            shutil.move(str(made[0]), dest)
        return dest

    if engine == "indextts2":
        if not _indextts_enabled():
            raise SynthError(
                "IndexTTS-2 is not a supported mdspeak runtime. "
                "Its model and voice rights must be verified separately."
            )
        snap = _indextts_snapshot()
        binary = _indextts_bin()
        reference = _indextts_reference()
        if not snap or not binary or not reference:
            raise SynthError(
                "IndexTTS-2 needs explicit MDSPEAK_INDEXTTS_COMMAND and "
                "MDSPEAK_INDEXTTS_REFERENCE_AUDIO configuration."
            )
        cmd = [
            binary, "generate",
            "-m", snap,
            "-r", _indextts_speaker(snap, binary, reference),
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


def _indextts_speaker(snap: Path, binary: Path, reference: Path) -> Path:
    """Pre-computed speaker conditioning: 9 s of load time down to 1.5 s."""
    if _SPEAKER_CACHE.exists():
        return _SPEAKER_CACHE
    _SPEAKER_CACHE.parent.mkdir(parents=True, exist_ok=True)
    _run([binary, "speaker", "-m", snap, "-r", reference, "-o", _SPEAKER_CACHE])
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
