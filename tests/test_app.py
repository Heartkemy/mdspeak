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

client = TestClient(app, base_url="http://127.0.0.1")


def test_engines_keep_kokoro_as_the_supported_default(monkeypatch, tmp_path):
    command = tmp_path / "mlx_audio.tts.generate"
    command.write_text("#!/bin/sh\nexit 0\n")
    command.chmod(0o755)
    monkeypatch.setenv("MDSPEAK_MLX_AUDIO_COMMAND", str(command))
    body = client.get("/api/engines").json()
    keys = {e["key"] for e in body["engines"]}
    assert keys == {"kokoro", "marvis"}
    assert all(e["available"] for e in body["engines"])
    assert body["engines"][0]["key"] == "kokoro"
    assert engine.KOKORO_DEFAULT_VOICE == "bm_fable"
    assert any(v["id"] == "bm_fable" for v in body["kokoroVoices"])


def test_kokoro_missing_runtime_has_actionable_error(monkeypatch, tmp_path):
    monkeypatch.delenv("MDSPEAK_MLX_AUDIO_COMMAND", raising=False)
    monkeypatch.setattr(engine.shutil, "which", lambda _name: None)
    with pytest.raises(engine.SynthError, match="MDSPEAK_MLX_AUDIO_COMMAND"):
        engine.synth_chunk("Synthetic text.", tmp_path / "out.wav", "kokoro")


def test_native_runtime_uses_explicit_interpreter_or_cli_shebang(monkeypatch, tmp_path):
    runtime = tmp_path / "python"
    runtime.write_text("#!/bin/sh\n")
    runtime.chmod(0o755)
    command = tmp_path / "mlx_audio.tts.generate"
    command.write_text(f"#!{runtime}\n")
    command.chmod(0o755)
    monkeypatch.setenv("MDSPEAK_MLX_AUDIO_COMMAND", str(command))
    monkeypatch.delenv("MDSPEAK_MLX_AUDIO_PYTHON", raising=False)
    assert engine.mlx_audio_python() == runtime


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
    assert 'id="calm"' in html  # calm mode toggle


def test_local_api_rejects_remote_targets_and_disables_http_caching():
    assert client.get("/api/engines", headers={"host": "example.test"}).status_code == 403
    assert client.post(
        "/api/preview",
        json={"markdown": "Synthetic text."},
        headers={"origin": "https://example.test"},
    ).status_code == 403
    response = client.get("/api/engines")
    assert response.headers["cache-control"] == "no-store"


def test_local_api_bounds_untrusted_text_size():
    response = client.post("/api/preview", json={"markdown": "x" * 50_001})
    assert response.status_code == 413


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


# ---------------------------------------------------------------- breathing room

def _tone(seconds, rate=24000):
    """A 440 Hz sine burst — speech stand-in with no internal silence."""
    import array
    import math

    a = array.array("h")
    for i in range(int(rate * seconds)):
        a.append(int(12000 * math.sin(2 * math.pi * 440 * i / rate)))
    return a


def _quiet(seconds, rate=24000):
    import array

    return array.array("h", b"\x00\x00") * int(rate * seconds)


def _write_wav(path, segments, rate=24000):
    import array

    flat = array.array("h")
    for s in segments:
        flat.extend(s)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(flat.tobytes())
    return path


def test_breathe_lengthens_only_internal_pauses(tmp_path):
    rate = 24000
    src = _write_wav(tmp_path / "src.wav", [_quiet(0.05), _tone(0.15), _quiet(0.2), _tone(0.15), _quiet(0.1)])

    engine.breathe(src, 0.4)

    # The 0.2 s internal gap grows to 0.6 s; lead/trail silence stays put.
    assert engine.wav_duration(src) == pytest.approx(0.05 + 0.15 + 0.6 + 0.15 + 0.1, abs=0.005)
    import array

    with wave.open(str(src), "rb") as w:
        frames = array.array("h", w.readframes(w.getnframes()))
    assert frames[: int(0.05 * rate)] == _quiet(0.05)
    assert max(abs(s) for s in frames) > 5000  # the tone survived the rewrite


def test_breathe_leaves_micro_and_boundary_silence(tmp_path):
    rate = 24000
    src = _write_wav(tmp_path / "micro.wav", [_tone(0.15), _quiet(0.02), _tone(0.15), _quiet(0.3)])

    before = engine.wav_duration(src)
    engine.breathe(src, 0.4)

    # 0.02 s is a consonant closure, 0.3 s trailing is boundary: neither grows.
    assert engine.wav_duration(src) == pytest.approx(before, abs=0.005)


def test_breathe_leaves_clause_commas_alone(tmp_path):
    """Real TTS output carries 30–100 ms gaps that are word/comma breaks, not
    sentence ends — widening them is what makes the voice sound cut off."""
    src = _write_wav(tmp_path / "clause.wav", [_tone(0.15), _quiet(0.1), _tone(0.15)])
    before = engine.wav_duration(src)

    engine.breathe(src, engine.CALM_PAUSE)

    assert engine.wav_duration(src) == pytest.approx(before, abs=0.005)


def test_breathe_caps_the_widened_gap(tmp_path):
    """Flat extra seconds on an already-long pause make a near-second hole.
    The widened gap is capped so quiet stays quiet, not robotic."""
    src = _write_wav(tmp_path / "cap.wav", [_tone(0.15), _quiet(0.6), _tone(0.15)])

    engine.breathe(src, engine.CALM_PAUSE)

    # 0.6 s natural + 0.6 s extra would be 1.2 s; the cap holds it at ~0.7 s.
    assert engine.wav_duration(src) == pytest.approx(0.15 + 0.7 + 0.15, abs=0.005)


def test_breathe_is_a_noop_with_zero_extra_and_silent_files(tmp_path):
    src = _write_wav(tmp_path / "z.wav", [_tone(0.1)])
    engine.breathe(src, 0.0)
    assert engine.wav_duration(src) == pytest.approx(0.1, abs=0.005)

    quiet = _write_wav(tmp_path / "quiet.wav", [_quiet(0.2)])
    engine.breathe(quiet, 0.4)
    assert engine.wav_duration(quiet) == pytest.approx(0.2, abs=0.005)


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
