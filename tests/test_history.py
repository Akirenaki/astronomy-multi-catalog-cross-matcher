from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.main import app


def test_history_links_to_object_profile_when_simbad_id_exists(monkeypatch):
    """History rows should link to the object profile when a canonical SIMBAD ID exists."""

    monkeypatch.setattr(
        "app.main.list_recent_objects",
        lambda limit=10: [
            SimpleNamespace(
                query_text="51 Peg",
                resolution_state="RESOLVED",
                simbad_main_id="51 Peg",
            )
        ],
    )

    with TestClient(app) as client:
        response = client.get("/history")

    assert response.status_code == 200
    assert 'href="/object/51%20Peg"' in response.text
