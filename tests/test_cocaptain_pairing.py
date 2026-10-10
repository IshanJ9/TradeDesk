import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

NOW = datetime(2026, 10, 10, 5, tzinfo=timezone.utc)
USERS = json.dumps([dict(id=id, display_name=name, email=f"{id}@example.invalid")
                    for id, name in [("a", "Trader"), ("b", "Ravi"), ("c", "Other")]])


def actor(id):
    return {"x-tradedesk-actor": id}


def settings(**updates):
    return Settings(**(dict(demo_mode=True, cocaptain_enabled=True, cocaptain_dev_actors=True,
                           cocaptain_account_owner_id="a", cocaptain_dev_users=USERS,
                           ticker_interval=None, reconcile_interval=None, external_sync_interval=None) | updates))


@pytest.fixture
def client():
    with TestClient(create_app(settings(), clock=lambda: NOW)) as c:
        yield c


def invite(c, email="b@example.invalid"):
    return c.post("/api/cocaptain/invite", headers=actor("a"), json={"email": email})


def action(c, link, who, action):
    return c.post(f"/api/cocaptain/{action}", headers=actor(who),
                  json={"owner_id": link["owner_id"], "link_id": link["id"]})


def test_accept_revoke_and_reinvite_invalidate_previous_generation(client):
    link = invite(client).json()
    assert link["status"] == "INVITED"
    assert invite(client).json()["id"] == link["id"]
    assert action(client, link, "a", "accept").status_code == 403
    assert action(client, link, "c", "accept").status_code == 403
    assert action(client, link, "b", "accept").json()["status"] == "ACTIVE"
    assert invite(client, "c@example.invalid").status_code == 409
    assert action(client, link, "c", "revoke").status_code == 403
    assert action(client, link, "b", "revoke").json()["status"] == "REVOKED"
    assert action(client, link, "b", "accept").status_code == 409
    fresh = invite(client).json()
    assert fresh["id"] != link["id"]
    assert action(client, link, "b", "accept").status_code == 404


def test_self_invitation_unknown_and_reviewer_invitation_refused(client):
    assert invite(client, "a@example.invalid").status_code == 403
    assert invite(client, "nobody@example.invalid").status_code == 404
    assert client.post("/api/cocaptain/invite", headers=actor("b"), json={"email": "c@example.invalid"}).status_code == 403


def test_gets_are_read_only_and_actions_require_post(client):
    link = invite(client).json()
    before = client.app.state.db.conn.total_changes
    for route in ("config", "settings"):
        assert client.get(f"/api/cocaptain/{route}", headers=actor("b")).status_code == 200
    for route in ("invite", "accept", "revoke"):
        assert client.get(f"/api/cocaptain/{route}", headers=actor("b")).status_code == 405
    assert client.app.state.db.conn.total_changes == before
    assert client.app.state.cocaptain_pairing.get("a").status == "INVITED"


@pytest.mark.parametrize("route", ["account", "orders", "pending", "profile", "discipline", "audit", "rules", "activity"])
def test_reviewer_cannot_read_trader_portfolio_or_history(client, route):
    response = client.get(f"/api/{route}", headers=actor("b"))
    assert response.status_code in (403, 404)


def test_identity_headers_are_disabled_by_default_and_without_demo():
    with pytest.raises(ValueError, match="DEMO_MODE"):
        create_app(settings(demo_mode=False))
    with TestClient(create_app(settings(cocaptain_dev_actors=False))) as c:
        assert c.get("/api/cocaptain/settings", headers=actor("a")).status_code == 503
    with TestClient(create_app(Settings(ticker_interval=None, external_sync_interval=None))) as c:
        assert c.get("/api/cocaptain/settings", headers=actor("a")).status_code == 404


def test_unknown_actor_denied_and_owner_access_preserved(client):
    assert client.get("/api/account", headers=actor("a")).status_code == 200
    assert client.get("/api/account").status_code == 401
    assert client.get("/api/cocaptain/settings", headers=actor("unknown")).status_code == 401


def test_audit_actor_ids_and_actor_scoped_notifications(client):
    hub = client.app.state.cocaptain_hub
    qa, qb, qc = (hub.subscribe(id) for id in ("a", "b", "c"))
    link = invite(client).json()
    assert qa.get_nowait()["action"] == "invited"
    assert qb.get_nowait()["action"] == "invited"
    assert qc.empty()
    action(client, link, "b", "accept")
    action(client, link, "a", "revoke")
    events = client.app.state.db.query("SELECT actor,data FROM audit_events WHERE kind='COCAPTAIN' ORDER BY seq")
    assert all(e["actor"] == "user" for e in events)
    assert [(json.loads(e["data"])["actor_id"], json.loads(e["data"])["action"]) for e in events] == [("a", "invited"), ("b", "accepted"), ("a", "revoked")]


def test_pairing_survives_restart(tmp_path):
    config = settings(database_url="sqlite:///"+str(tmp_path/"pairing.db"))
    with TestClient(create_app(config, clock=lambda: NOW)) as c:
        link = invite(c).json()
        action(c, link, "b", "accept")
    with TestClient(create_app(config, clock=lambda: NOW)) as c:
        restored = c.get("/api/cocaptain/settings", headers=actor("b")).json()
        assert restored["links"][0]["id"] == link["id"] and restored["links"][0]["status"] == "ACTIVE"
