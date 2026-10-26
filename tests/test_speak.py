"""Worker (speak) entry-point tests — no model load.

These exercise the FastAPI routes of ``mdspeak.speak`` without touching the
mlx-audio venv. The empty-text path and /health are the two routes that answer
before the model-resident generator is spawned, so they stay fast.
"""

import io
import json
import threading
import wave
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from mdspeak import engine, speak
from mdspeak.speak import app

client = TestClient(app, base_url="http://127.0.0.1")


def test_health_reports_worker_status_shape():
    r = client.get("/health").json()
    assert set(r) == {"ok", "ready", "error"}
    assert isinstance(r["ok"], bool)
    assert isinstance(r["ready"], bool)
    assert isinstance(r["error"], str)


def test_speak_rejects_empty_text():
    assert client.post("/speak", json={"text": "   \n"}).status_code == 400


def test_voices_lists_kokoro_by_default_and_retains_other_engines(monkeypatch):
    r = client.get("/voices").json()
    assert r["voices"][0]["id"] == "bm_fable"
    monkeypatch.setattr(speak, "_default_engine", "gemini")
    r = client.get("/voices").json()
    assert "Charon" in {v["id"] for v in r["voices"]}
    monkeypatch.setattr(speak, "_default_engine", "kokoro")
    r = client.get("/voices").json()
    ids = {v["id"] for v in r["voices"]}
    assert "bm_fable" in ids
    assert "bm_fable" in ids
    assert all({"id", "label"} <= set(v) for v in r["voices"])


@pytest.mark.parametrize("provider, voice", [
    ("kokoro", "bm_fable"), ("gemini", "Charon"), ("cloudflare", "jupiter"),
])
def test_native_can_select_voices_without_restarting_worker(provider, voice):
    response = client.get("/voices", params={"engine": provider})
    assert response.status_code == 200
    assert voice in {v["id"] for v in response.json()["voices"]}


@pytest.mark.parametrize("voice", ["", "bm_george"])
def test_native_can_speak_locally_with_safe_default_and_no_credentials(monkeypatch, tmp_path, voice):
    monkeypatch.setattr(speak, "_default_engine", "kokoro")
    monkeypatch.setattr(speak, "_default_voice", "bm_fable")
    monkeypatch.setattr(speak, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(speak.engine, "mlx_audio_python", lambda: tmp_path / "python")
    def no_cloud():
        pytest.fail("Selecting Kokoro must not require cloud credentials")
    monkeypatch.setattr(speak.engine, "cloudflare_credentials", no_cloud)
    # Replace only the model process; exercise the real request/render/segment path.
    wav = tmp_path / "local.wav"
    with wave.open(str(wav), "wb") as output:
        output.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        output.writeframes(b"\x00\x20" * 2400)
    audio = wav.read_bytes()
    stdin = io.StringIO()
    monkeypatch.setattr(speak, "_proc", SimpleNamespace(
        stdin=stdin, stdout=io.StringIO(json.dumps({"ok": True, "path": str(wav)}) + "\n")))
    monkeypatch.setattr(speak, "_ensure_proc", lambda: None)
    ready = threading.Event()
    ready.set()
    monkeypatch.setattr(speak, "_ready", ready)
    monkeypatch.setattr(speak, "_proc_ready", True)
    result = client.post("/speak", json={"text": "# Local **speech**", "engine": "kokoro", "voice": voice})
    assert result.status_code == 200
    response = client.get(f'/segment/{result.json()["id"]}/0.wav')
    assert response.status_code == 200
    assert response.content == audio
    request = json.loads(stdin.getvalue())
    assert request["text"] == "Local speech."
    assert request["voice"] == (voice or "bm_fable")


def test_worker_rejects_remote_requests_oversized_text_and_missing_runtime(monkeypatch):
    assert client.get("/health", headers={"host": "example.test"}).status_code == 403
    assert client.post(
        "/speak",
        json={"text": "Synthetic text."},
        headers={"origin": "https://example.test"},
    ).status_code == 403
    assert client.post("/speak", json={"text": "x" * 50_001}).status_code == 413
    monkeypatch.setattr(engine, "mlx_audio_python", lambda: None)
    response = client.post("/speak", json={"text": "Synthetic text."})
    assert response.status_code == 503
    assert "MDSPEAK_MLX_AUDIO_PYTHON" in response.json()["detail"]


def test_native_rejects_unknown_engine():
    assert client.get("/voices?engine=unknown").status_code == 422
    assert client.post("/speak", json={"text": "Read me.", "engine": "unknown"}).status_code == 422
