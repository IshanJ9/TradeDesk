"""Pairing between real accounts: an invitation by email, accepted by the invited person, ended by either. A new link
id is a new approval generation. Anyone with an account can invite one other account and be invited; nothing here
lets one person act as another. (Ryan's original cases, ported from the demo identities to signed-in users.)"""

import json
from datetime import datetime, timezone

import pytest
from conftest import SignedInClient

from app.config import Settings
from app.main import create_app

NOW = datetime(2026, 10, 10, 5, tzinfo=timezone.utc)


def settings(**updates):
    return Settings(**(dict(cocaptain_enabled=True, ticker_interval=None, reconcile_interval=None,
                            external_sync_interval=None) | updates))


class People:
    def __init__(self, app, a):
        self.app = app
        self.a = a
        self.b, self.c = SignedInClient(app, email="b@example.com"), SignedInClient(app, email="c@example.com")
        self.b.portal = self.c.portal = a.portal
        for client in (a, self.b, self.c):
            client.sign_in()
        self.ids = {k: v.get("/api/auth/me").json()["user"]["id"] for k, v in (("a", a), ("b", self.b), ("c", self.c))}

    def who(self, key):
        return getattr(self, key)

    def invite(self, email="b@example.com", by="a"):
        return self.who(by).post("/api/cocaptain/invite", json={"email": email})

    def action(self, link, who, action):
        return self.who(who).post(f"/api/cocaptain/{action}", json={"owner_id": link["owner_id"], "link_id": link["id"]})


@pytest.fixture
def people():
    app = create_app(settings(), clock=lambda: NOW)
    with SignedInClient(app, email="a@example.com") as a:
        yield People(app, a)


def test_accept_revoke_and_reinvite_invalidate_previous_generation(people):
    link = people.invite().json()
    assert link["status"] == "INVITED"
    assert people.invite().json()["id"] == link["id"]  # inviting the same person again changes nothing
    assert people.action(link, "a", "accept").status_code == 403  # the inviter cannot accept for them
    assert people.action(link, "c", "accept").status_code == 403  # nor can a stranger
    assert people.action(link, "b", "accept").json()["status"] == "ACTIVE"
    assert people.invite("c@example.com").status_code == 409  # one Co-Captain at a time
    assert people.action(link, "c", "revoke").status_code == 403  # only the two people in it can end it
    assert people.action(link, "b", "revoke").json()["status"] == "REVOKED"
    assert people.action(link, "b", "accept").status_code == 409
    fresh = people.invite().json()
    assert fresh["id"] != link["id"]  # a new link id: nothing approved under the old one carries over
    assert people.action(link, "b", "accept").status_code == 404


def test_self_invitation_unknown_and_second_invitation_refused(people):
    assert people.invite("a@example.com").status_code == 403
    assert people.invite("nobody@example.com").status_code == 404  # only real accounts can be invited
    link = people.invite().json()
    people.action(link, "b", "accept")
    assert people.invite("b@example.com", by="b").status_code == 403  # b cannot invite themselves either
    assert people.invite("c@example.com", by="b").status_code == 200  # but b has their own desk and may invite c


def test_each_person_sees_only_their_own_links_and_the_names_in_them(people):
    link = people.invite().json()
    people.action(link, "b", "accept")
    mine, theirs, stranger = (people.who(k).get("/api/cocaptain/settings").json() for k in ("a", "b", "c"))
    assert [x["id"] for x in mine["links"]] == [x["id"] for x in theirs["links"]] == [link["id"]]
    assert stranger["links"] == []
    assert {p["id"] for p in mine["people"]} == {people.ids["a"], people.ids["b"]}
    assert {p["id"] for p in stranger["people"]} == {people.ids["c"]}


def test_gets_are_read_only_and_actions_require_post(people):
    people.invite()
    before = people.app.state.db.conn.total_changes
    for route in ("config", "settings"):
        assert people.b.get(f"/api/cocaptain/{route}").status_code == 200
    for route in ("invite", "accept", "revoke"):
        assert people.b.get(f"/api/cocaptain/{route}").status_code == 405
    assert people.app.state.db.conn.total_changes == before
    assert people.app.state.cocaptain_pairing.get(people.ids["a"]).status == "INVITED"


@pytest.mark.parametrize("route", ["account", "orders", "pending", "profile", "discipline", "audit", "rules", "activity"])
def test_a_co_captain_still_cannot_read_the_traders_portfolio_or_history(people, route):
    link = people.invite().json()
    people.action(link, "b", "accept")
    a_card = people.a.post("/api/orders/preview", json=dict(action="PLACE", instrument_ref="ITC", side="BUY", quantity=1,
                                                             order_type="MARKET")).json()["cards"][0]["pending"]
    body = json.dumps(people.b.get(f"/api/{route}").json())  # their OWN desk's answer
    assert a_card["id"] not in body


def test_nothing_works_without_signing_in(people):
    from starlette.testclient import TestClient as Anonymous
    anon = Anonymous(people.app)
    for method, path in (("GET", "/api/cocaptain/config"), ("GET", "/api/cocaptain/settings"), ("GET", "/api/cocaptain/inbox"),
                         ("POST", "/api/cocaptain/invite"), ("POST", "/api/cocaptain/accept"), ("POST", "/api/cocaptain/revoke")):
        assert anon.request(method, path, json={} if method == "POST" else None).status_code == 401, path


def test_a_disabled_account_cannot_be_invited(people):
    people.app.state.auth.store.set_disabled(people.ids["c"], True)
    assert people.invite("c@example.com").status_code == 404


def test_the_feature_is_off_unless_switched_on():
    app = create_app(Settings(ticker_interval=None, reconcile_interval=None, external_sync_interval=None), clock=lambda: NOW)
    with SignedInClient(app, email="a@example.com") as c:
        assert c.get("/api/cocaptain/config").json() == {"enabled": False}
        for method, path in (("GET", "/api/cocaptain/settings"), ("GET", "/api/cocaptain/inbox"),
                             ("POST", "/api/cocaptain/invite")):
            assert c.request(method, path, json={"email": "b@example.com"} if method == "POST" else None).status_code == 404


def test_audit_records_who_did_each_step_in_the_traders_log(people):
    link = people.invite().json()
    people.action(link, "b", "accept")
    people.action(link, "a", "revoke")
    ws = people.app.state.workspaces.peek(people.ids["a"])
    from app.schemas import AuditKind
    rows = list(reversed(ws.audit.list(50, kind=AuditKind.COCAPTAIN)))
    assert all(r.actor == "user" for r in rows)
    assert [(r.data["actor_id"], r.data["action"]) for r in rows] == [
        (people.ids["a"], "invited"), (people.ids["b"], "accepted"), (people.ids["a"], "revoked")]


def test_pairing_survives_restart(tmp_path):
    config = settings(database_url="sqlite:///" + str(tmp_path / "pairing.db"))
    app = create_app(config, clock=lambda: NOW)
    with SignedInClient(app, email="a@example.com") as a:
        p = People(app, a)
        link = p.invite().json()
        p.action(link, "b", "accept")
    app2 = create_app(config, clock=lambda: NOW)
    with SignedInClient(app2, email="a@example.com") as a2:
        p2 = People(app2, a2)
        restored = p2.b.get("/api/cocaptain/settings").json()
        assert restored["links"][0]["id"] == link["id"] and restored["links"][0]["status"] == "ACTIVE"
