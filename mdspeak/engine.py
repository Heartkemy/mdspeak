"""Local MLX synthesis, with optional separately configured cloud engines.

Kokoro is the supported local baseline.  The application deliberately does not
bundle a Python interpreter, model weights, a reference voice, or cloud
credentials: those belong to separately installed/configured services and
remain governed by their upstream terms.
"""

from __future__ import annotations

import base64
import binascii
import glob
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import textwrap
import urllib.error
import urllib.request
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
    ("bm_lewis", "Lewis — British male"),
    ("bm_george", "George — British male, low and steady"),
    ("bm_daniel", "Daniel — British male, soft-spoken"),
    ("am_michael", "Michael — American male, gentle"),
    ("am_onyx", "Onyx — American male, deep"),
    ("af_heart", "Heart — American female, warm"),
    ("af_nicole", "Nicole — American female, hushed"),
    ("bf_emma", "Emma — British female, calm"),
]
KOKORO_DEFAULT_VOICE = "bm_fable"

GEMINI_MODEL = "gemini-3.8-flash-tts"
GEMINI_DEFAULT_VOICE = "Charon"
GEMINI_VOICES = [
    ("Charon", "Charon — informative"),
    ("Kore", "Kore — firm"),
    ("Sulafat", "Sulafat — warm"),
    ("Achernar", "Achernar — soft"),
    ("Puck", "Puck — upbeat"),
]
CLOUDFLARE_MODEL = "@cf/deepgram/aura-2-en"
CLOUDFLARE_DEFAULT_VOICE = "jupiter"
CLOUDFLARE_VOICES = [(v, v.capitalize()) for v in ("jupiter", "luna", "orion", "athena", "zeus", "apollo")]
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def _env(*names: str) -> str:
    """Read only named settings, independent of the launcher's cwd or shell."""
    for name in names:
        if value := os.environ.get(name, "").strip():
            return value
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            name, sep, value = line.strip().removeprefix("export ").partition("=")
            if sep and name.strip() in names:
                try:
                    values = shlex.split(value, comments=True)
                except ValueError:
                    raise SynthError(f"Invalid {name.strip()} entry in .env") from None
                if values:
                    return values[0]
    return ""


def gemini_api_key() -> str:
    return _env("GEMINI_API_KEY", "GOOGLE_API_KEY")


def cloudflare_credentials() -> tuple[str, str]:
    token, account = _env("CLOUDFLARE_API_TOKEN"), _env("CLOUDFLARE_ACCOUNT_ID")
    if not token or not account:
        raise SynthError("Set CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID in the project .env file")
    if not re.fullmatch(r"[a-fA-F0-9]{32}", account):
        raise SynthError("CLOUDFLARE_ACCOUNT_ID must be the 32-character account ID from the dashboard")
    return token, account


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
    """Configured engines without loading a model, downloading weights, or probing quota."""
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
            note="experimental local alternative",
            available=mlx_audio_command() is not None,
            detail=MARVIS_REPO,
            suggested=False,
        ),
        Engine(
            key="cloudflare",
            label="Cloudflare Aura 2",
            note="optional cloud speech — text leaves this machine",
            available=bool(_env("CLOUDFLARE_API_TOKEN") and _env("CLOUDFLARE_ACCOUNT_ID")),
            detail=CLOUDFLARE_MODEL,
        ),
        Engine(
            key="gemini",
            label="Gemini 3.8 Flash TTS",
            note="optional cloud speech — text is sent to Google",
            available=bool(gemini_api_key()),
            detail=GEMINI_MODEL,
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
    return any(choice.key == key and choice.batches for choice in engines())


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
        # A sentence (or a single long token) can exceed the service input bound.
        pieces = [part for sentence in _sentences(block)
                  for part in textwrap.wrap(sentence, limit, break_on_hyphens=False)]
        for piece in pieces:
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
    if engine == "cloudflare":
        return _cloudflare(text, dest, voice)
    if engine == "gemini":
        return _gemini(text, dest, voice)
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


def _cloudflare(text: str, dest: Path, voice: str) -> Path:
    token, account = cloudflare_credentials()
    request = urllib.request.Request(
        f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{CLOUDFLARE_MODEL}",
        data=json.dumps({"text": text, "speaker": voice or CLOUDFLARE_DEFAULT_VOICE,
                         "encoding": "linear16", "container": "none", "sample_rate": 24000}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            if "json" in response.headers.get("Content-Type", "").lower():
                raise SynthError("Cloudflare Aura returned an error instead of audio")
            data = response.read()
    except urllib.error.HTTPError as exc:
        # Never echo provider responses that might include credentials or input.
        if exc.code == 429:
            raise SynthError("Cloudflare Aura quota/rate limit reached (HTTP 429); wait for the allowance to reset") from None
        raise SynthError(f"Cloudflare Aura failed (HTTP {exc.code}); check token permissions, account ID and Workers AI access") from None
    except (urllib.error.URLError, TimeoutError):
        raise SynthError("Cloudflare Aura connection failed or timed out") from None
    except ValueError:
        raise SynthError("Cloudflare Aura request or response was invalid") from None
    if not data or len(data) % 2:
        raise SynthError("Cloudflare Aura returned empty or incomplete PCM audio")
    # Aura's streamed WAV uses a placeholder size; wrap raw PCM with a final length.
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        wav.writeframes(data)
    return _write_wav(buffer.getvalue(), dest, "Cloudflare Aura")


def _gemini(text: str, dest: Path, voice: str) -> Path:
    """Gemini 3.8 Interactions API returns a complete mono PCM WAV."""
    key = gemini_api_key()
    if not key:
        raise SynthError("Set GEMINI_API_KEY in the project .env file or environment")
    payload = {
        "model": GEMINI_MODEL,
        "input": [{"type": "user_input", "content": [{"type": "text", "text": text}]}],
        "response_format": {"type": "audio"},
        "generation_config": {"speech_config": [{"voice": voice or GEMINI_DEFAULT_VOICE}]},
        "store": False,
    }
    request = urllib.request.Request(
        "https://generativelanguage.googleapis.com/v1beta/interactions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "x-goog-api-key": key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        # Never surface raw responses/headers, which may echo credentials or input.
        if exc.code == 429:
            raise SynthError("Gemini TTS quota exhausted (HTTP 429). Wait for the quota reset or enable a higher API tier.") from None
        raise SynthError(f"Gemini TTS failed (HTTP {exc.code}); check API key, model access and quota") from None
    except (urllib.error.URLError, TimeoutError):
        raise SynthError("Gemini TTS connection failed or timed out") from None
    except ValueError:
        raise SynthError("Gemini TTS returned an invalid response") from None

    try:
        audio = [part for step in result.get("steps", []) if step.get("type") == "model_output"
                 for part in step.get("content", []) if part.get("type") == "audio"]
        if not audio:
            raise SynthError("Gemini TTS returned no audio; the request may have been blocked")
        data = base64.b64decode(audio[-1]["data"], validate=True)
    except (AttributeError, KeyError, TypeError, ValueError, binascii.Error):
        raise SynthError("Gemini TTS returned invalid WAV audio") from None
    return _write_wav(data, dest, "Gemini TTS")


def _write_wav(data: bytes, dest: Path, provider: str) -> Path:
    """Validate the shared playback/Calm format before publishing cloud audio."""
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 24000):
                raise SynthError(f"{provider} returned an unsupported WAV format")
            if not wav.getnframes() or len(wav.readframes(wav.getnframes())) != wav.getnframes() * 2:
                raise SynthError(f"{provider} returned empty or incomplete audio")
    except (wave.Error, EOFError):
        raise SynthError(f"{provider} returned invalid WAV audio") from None
    dest.write_bytes(data)
    return dest


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
