"""API, chunking and wav-joining tests.

The end-to-end synthesis test is opt-in (`-m slow`) because it loads a real
model and takes minutes. Everything else runs in milliseconds.
"""

import wave

import pytest
from fastapi.testclient import TestClient

from mdspeak import engine
from mdspeak.server import app
from tests.test_normalize import AGENT_OUTPUT

client = TestClient(app)


def test_engines_reports_disk_reality():
    body = client.get("/api/engines").json()
    keys = {e["key"] for e in body["engines"]}
    assert keys == {"kokoro", "marvis", "indextts2"}
    # At least one engine must actually be runnable, or the app is a shell.
    assert any(e["available"] for e in body["engines"])
    assert "calm" in body["emotions"]
    # Kokoro is the default and needs a voice list to pick from.
    assert body["engines"][0]["key"] == "kokoro"
    assert any(v["id"] == "bm_fable" for v in body["kokoroVoices"])


def test_preview_is_clean_and_chunked():
    r = client.post("/api/preview", json={"markdown": AGENT_OUTPUT}).json()
    assert r["leaks"] == []
    assert r["chunks"] >= 1
    assert "code block" in r["text"]


def test_preview_read_code_toggle():
    off = client.post("/api/preview", json={"markdown": AGENT_OUTPUT}).json()
    on = client.post(
        "/api/preview", json={"markdown": AGENT_OUTPUT, "read_code": True}
    ).json()
    assert "AuthError" not in off["text"]
    assert "AuthError" in on["text"]
    assert on["leaks"] == []


def test_speak_rejects_empty():
    assert client.post("/api/speak", json={"markdown": "   \n\n"}).status_code == 400


def test_job_404():
    assert client.get("/api/job/nope").status_code == 404
    assert client.get("/api/audio/nope.wav").status_code == 404


def test_index_serves_the_page():
    html = client.get("/").text
    assert "<title>mdspeak</title>" in html
    assert "playbackRate" in html  # speed control
    assert 'id="scrub"' in html  # seek control


# ---------------------------------------------------------------- chunking

def test_chunking_respects_limit_and_keeps_all_words():
    text = " ".join(f"Sentence number {i} is here." for i in range(60))
    parts = engine.chunk(text, limit=120)
    assert len(parts) > 1
    assert all(len(p) <= 200 for p in parts)  # a single sentence may overshoot
    assert " ".join(parts).split() == text.split()


def test_chunking_splits_on_blank_lines():
    assert engine.chunk("Heading.\n\nBody text.") == ["Heading.", "Body text."]


def test_chunking_never_emits_empty():
    assert all(p.strip() for p in engine.chunk("\n\n  \n\nA.\n\n\n"))


def test_long_unpunctuated_block_still_chunks():
    parts = engine.chunk("word " * 400, limit=200)
    assert len(parts) >= 1
    assert all(p.strip() for p in parts)


# ---------------------------------------------------------------- wav joining

def _wav(path, seconds=0.1, rate=24000):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x01" * int(rate * seconds))
    return path


def test_join_wavs_sums_duration(tmp_path):
    parts = [_wav(tmp_path / f"{i}.wav", 0.2) for i in range(3)]
    out = engine.join_wavs(parts, tmp_path / "all.wav")
    assert engine.wav_duration(out) == pytest.approx(0.6, abs=0.01)


def test_join_wavs_single_part(tmp_path):
    out = engine.join_wavs([_wav(tmp_path / "a.wav", 0.3)], tmp_path / "o.wav")
    assert engine.wav_duration(out) == pytest.approx(0.3, abs=0.01)


def test_join_wavs_rejects_mismatched_rates(tmp_path):
    parts = [_wav(tmp_path / "a.wav", 0.1, 24000), _wav(tmp_path / "b.wav", 0.1, 22050)]
    with pytest.raises(engine.SynthError):
        engine.join_wavs(parts, tmp_path / "o.wav")


def test_join_wavs_needs_input(tmp_path):
    with pytest.raises(engine.SynthError):
        engine.join_wavs([], tmp_path / "o.wav")


# ---------------------------------------------------------------- end to end

@pytest.mark.slow
def test_end_to_end_produces_real_audio(tmp_path):
    """Loads the real model. Asserts on the waveform, not on an exit code."""
    import array

    out = engine.synth_chunk(
        "The refactor touched three files and all the tests pass.",
        tmp_path / "e2e.wav",
        "marvis",
    )
    dur = engine.wav_duration(out)
    assert 1.5 < dur < 20, f"implausible duration {dur}"
    with wave.open(str(out), "rb") as w:
        frames = array.array("h", w.readframes(w.getnframes()))
    peak = max(abs(s) for s in frames)
    rms = (sum(s * s for s in frames) / len(frames)) ** 0.5
    assert peak > 3000, f"audio is near-silent (peak {peak})"
    assert rms > 200, f"audio has no energy (rms {rms})"
