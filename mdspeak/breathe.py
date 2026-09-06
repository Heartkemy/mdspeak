"""Widen the natural pauses in rendered speech — the "breathing room" pass.

Reading back a dense agent summary loses people at the joins: sentences land
back-to-back with no time to register one before the next starts. A TTS model
already inserts short silences at punctuation; this lengthens those gaps rather
than inventing pauses mid-word, so nothing about the reading rhythm is faked.

Standard-library only (no mlx-audio import), so it is testable outside any
synthesis venv and safe to run on the output of any PCM engine.

Canonical source. `read-aloud/python/breathe.py` is a vendored copy — keep the
two in sync by hand, or re-copy together, exactly like `normalize.py`.

Amounts, in seconds, of extra silence to add after each detected pause:

    DEFAULT_PAUSE  0.25  gentle, applied to every read
    CALM_PAUSE      0.6  the heavier version a Calm mode asks for
"""

from __future__ import annotations

import array
import sys
import wave
from pathlib import Path

DEFAULT_PAUSE = 0.25
CALM_PAUSE = 0.6

# Only gaps this long get widened. Measured on real Kokoro output: a paragraph
# carries a scattering of 30–100 ms holes that are clause commas and word
# boundaries, not sentence breaks — lengthen those and the voice sounds like it
# is being cut off mid-thought. Real sentence ends are ~150–450 ms.
_PAUSE_MIN_S = 0.15
# A widened pause never grows past this. Adding a flat 0.6 s to a 0.4 s pause
# makes a near-second hole after every sentence, which reads as abrupt rather
# than calm; once a pause is long enough, extra silence buys nothing.
_PAUSE_MAX_S = 0.7


def breathe(wav: str | Path, extra: float) -> Path:
    """Widen the natural silences inside `wav` by up to `extra` seconds each.

    Returns the path (the file is rewritten in place). Only genuine prosodic
    pauses (>= 150 ms) are touched, so clause commas and word boundaries are
    left alone; the added silence is capped so no single gap grows past
    `_PAUSE_MAX_S`. Leading and trailing silence never change.
    """
    wav = Path(wav)
    if extra <= 0:
        return wav
    with wave.open(str(wav), "rb") as w:
        nch, sw, rate, nf, _, _ = w.getparams()
        raw = w.readframes(nf)
    if sw != 2:
        raise ValueError(f"breathe expects 16-bit PCM, got {sw * 8}-bit")
    frames = array.array("h", raw)
    if not frames:
        return wav
    if sys.byteorder != "little":
        frames.byteswap()
    peak = max(abs(s) for s in frames)
    if peak == 0:
        return wav

    # A frame counts as silence only if every channel is quiet. Threshold sits
    # ~2% of the file's own peak, so it tracks engine-specific loudness.
    thr = max(40, min(800, int(peak * 0.02)))
    total_frames = len(frames) // nch
    signal = bytearray(total_frames)  # 1 = has sound
    k = 0
    for base in range(0, len(frames), nch):
        loud = False
        for c in range(base, base + nch):
            if abs(frames[c]) > thr:
                loud = True
                break
        signal[k] = 1 if loud else 0
        k += 1

    out = array.array("h")
    sample_i = 0
    fi = 0
    while fi < total_frames:
        if signal[fi] == 0:
            start = fi
            while fi < total_frames and signal[fi] == 0:
                fi += 1
            dur = (fi - start) / rate
            internal = start > 0 and fi < total_frames
            if internal and dur >= _PAUSE_MIN_S:
                # Scale by what the pause needs: never push a gap past the cap,
                # and give a short real pause less than a long one, so the
                # widening reads as the same speaker pausing longer — not as
                # the audio stopping.
                add = min(extra, max(0.0, _PAUSE_MAX_S - dur))
                if add > 0:
                    end_sample = fi * nch
                    out.extend(frames[sample_i:end_sample])
                    zeros = array.array("h", b"\x00\x00") * int(round(add * rate * nch))
                    out.extend(zeros)
                    sample_i = end_sample
        else:
            fi += 1
    out.extend(frames[sample_i:])

    with wave.open(str(wav), "wb") as w:
        w.setnchannels(nch)
        w.setsampwidth(sw)
        w.setframerate(rate)
        w.setcomptype("NONE", "not compressed")
        w.writeframes(out.tobytes())
    return wav
