"""Worker (speak) entry-point tests — no model load.

These exercise the FastAPI routes of ``mdspeak.speak`` without touching the
mlx-audio venv. The empty-text path and /health are the two routes that answer
before the model-resident generator is spawned, so they stay fast.
"""

from fastapi.testclient import TestClient

from mdspeak.speak import app

client = TestClient(app)


def test_health_reports_worker_status_shape():
    r = client.get("/health").json()
    assert set(r) == {"ok", "ready", "error"}
    assert isinstance(r["ok"], bool)
    assert isinstance(r["ready"], bool)
    assert isinstance(r["error"], str)


def test_speak_rejects_empty_text():
    assert client.post("/speak", json={"text": "   \n"}).status_code == 400


def test_voices_lists_curated_kokoro_voices():
    r = client.get("/voices").json()
    ids = {v["id"] for v in r["voices"]}
    assert "bm_lewis" in ids
    assert "bm_fable" in ids
    assert all({"id", "label"} <= set(v) for v in r["voices"])
