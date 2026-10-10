from datetime import datetime, timedelta, timezone
import json

import pytest

from app.audit import AuditLog
from app.cocaptain.actors import Actor, DevDirectory
from app.cocaptain.events import ReviewHub
from app.cocaptain.pairing import Pairing
from app.cocaptain.store import ReviewError, ReviewStore
from app.db import Database


NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)
A = Actor(id="a", display_name="Trader")
B = Actor(id="b", display_name="Reviewer")
C = Actor(id="c", display_name="Stranger")


def setup(db, clock=lambda: NOW):
    directory = DevDirectory(json.dumps([
        {"id": a.id, "display_name": a.display_name, "email": f"{a.id}@example.invalid"}
        for a in (A, B, C)]))
    audit = AuditLog(db, clock)
    pairing = Pairing(db, directory, audit, ReviewHub(), clock)
    store = ReviewStore(db, pairing, audit, clock)
    pairing.on_revoke = store.revoke_link
    return pairing, store


def bindings(**changes):
    return dict(owner_id="a", account_id="account-a", order_hash="exact-fingerprint",
                expires_at=NOW + timedelta(minutes=1)) | changes


@pytest.fixture
def review():
    db = Database()
    pairing, store = setup(db)
    link = pairing.invite(A, "b@example.invalid")
    pairing.accept(B, "a", link.id)
    store.open(card_id="order-1", kind="ORDER", **bindings())
    yield db, pairing, store
    db.close()


@pytest.mark.parametrize("people", [(A, B), (B, A)])
def test_both_click_orders_and_duplicates(review, people):
    db, _, store = review
    assert not store.ready("order-1", **bindings())
    first = store.decide("order-1", people[0], "APPROVE", **bindings())
    assert not store.ready("order-1", **bindings())
    assert store.decide("order-1", people[0], "APPROVE", **bindings()) == first
    store.decide("order-1", people[1], "APPROVE", **bindings())
    assert store.ready("order-1", **bindings())
    assert len(store.decisions("order-1")) == 2
    assert db.query("SELECT count(*) n FROM executions")[0]["n"] == 0


def test_owner_click_never_fills_reviewer_role_and_stranger_cannot_decide(review):
    _, _, store = review
    store.decide("order-1", A, "APPROVE", **bindings())
    assert [d.role for d in store.decisions("order-1")] == ["TRADER"]
    with pytest.raises(ReviewError, match="Only the trader"):
        store.decide("order-1", C, "APPROVE", **bindings())
    assert not store.ready("order-1", **bindings())


@pytest.mark.parametrize("change", [dict(owner_id="b"), dict(account_id="account-b"),
    dict(order_hash="different"), dict(expires_at=NOW + timedelta(minutes=2))])
def test_binding_changes_cannot_record_decision(review, change):
    _, _, store = review
    with pytest.raises(ReviewError, match="does not match"):
        store.decide("order-1", B, "APPROVE", **bindings(**change))
    assert store.decisions("order-1") == []


def test_policy_change_cannot_reuse_approval(review):
    _, _, store = review
    with pytest.raises(ReviewError):
        store.ready("order-1", **bindings(), policy_version="zone-v2")


def test_revoke_invalidates_and_reinvite_does_not_restore_decisions(review):
    _, pairing, store = review
    store.decide("order-1", A, "APPROVE", **bindings())
    store.decide("order-1", B, "APPROVE", **bindings())
    pairing.revoke(A, "a", pairing.get("a").id)
    assert store.get("order-1").status == "INVALIDATED"
    with pytest.raises(ReviewError):
        store.ready("order-1", **bindings())
    link = pairing.invite(A, "b@example.invalid")
    pairing.accept(B, "a", link.id)
    with pytest.raises(ReviewError):
        store.decide("order-1", B, "APPROVE", **bindings())
    store.open(card_id="order-2", kind="ORDER", **bindings())
    assert not store.ready("order-2", **bindings())


def test_link_checked_even_if_invalidation_callback_missing(review):
    _, pairing, store = review
    pairing.on_revoke = None
    pairing.revoke(A, "a", pairing.get("a").id)
    with pytest.raises(ReviewError, match="not active"):
        store.decide("order-1", B, "APPROVE", **bindings())


def test_decline_is_terminal_and_duplicate_is_idempotent(review):
    _, _, store = review
    store.decide("order-1", A, "APPROVE", **bindings())
    first = store.decide("order-1", B, "DECLINE", **bindings())
    assert store.decide("order-1", B, "DECLINE", **bindings()) == first
    assert store.get("order-1").status == "DECLINED"
    with pytest.raises(ReviewError):
        store.ready("order-1", **bindings())
    with pytest.raises(ReviewError):
        store.decide("order-1", B, "APPROVE", **bindings())


def test_expiry_at_exact_boundary_is_dead_without_mutating_read(review):
    db, _, store = review
    store.clock = lambda: NOW + timedelta(minutes=1)
    changes = db.conn.total_changes
    with pytest.raises(ReviewError, match="expired"):
        store.ready("order-1", **bindings())
    assert db.conn.total_changes == changes
    with pytest.raises(ReviewError, match="expired"):
        store.decide("order-1", A, "APPROVE", **bindings())


def test_binding_is_immutable_and_requote_starts_fresh(review):
    _, _, store = review
    store.decide("order-1", A, "APPROVE", **bindings())
    with pytest.raises(ReviewError):
        store.open(card_id="order-1", kind="ORDER", **bindings(order_hash="new"))
    store.invalidate("order-1", "Requote required")
    store.open(card_id="order-2", kind="ORDER", **bindings(order_hash="new"))
    assert not store.ready("order-2", **bindings(order_hash="new"))


def test_restart_restores_plan_binding_and_same_approvals(tmp_path):
    url = "sqlite:///" + str(tmp_path / "reviews.db")
    db = Database(url)
    pairing, store = setup(db)
    link = pairing.invite(A, "b@example.invalid")
    pairing.accept(B, "a", link.id)
    store.open(card_id="plan-1", kind="PLAN", **bindings())
    store.decide("plan-1", B, "APPROVE", **bindings())
    db.close()
    db = Database(url)
    _, restored = setup(db)
    assert restored.get("plan-1").kind == "PLAN"
    assert not restored.ready("plan-1", **bindings())
    restored.decide("plan-1", A, "APPROVE", **bindings())
    assert restored.ready("plan-1", **bindings())
    db.close()


@pytest.mark.parametrize("column,value", [("order_hash", "tampered"), ("approver_id", "c"),
                                         ("policy_version", "zone-v0")])
def test_final_read_checks_each_persisted_decision_binding(review, column, value):
    db, _, store = review
    for actor in (A, B):
        store.decide("order-1", actor, "APPROVE", **bindings())
    # Column comes from this test's fixed parameter list, never user input.
    db.execute(f"UPDATE approvals SET {column}=? WHERE role='CO_CAPTAIN'", (value,))
    assert not store.ready("order-1", **bindings())


def test_readiness_and_decision_reads_do_not_mutate_and_audit_has_human_ids(review):
    db, _, store = review
    for actor in (A, B):
        store.decide("order-1", actor, "APPROVE", **bindings())
    changes = db.conn.total_changes
    assert store.ready("order-1", **bindings())
    store.get("order-1")
    store.decisions("order-1")
    assert db.conn.total_changes == changes
    events = [json.loads(row["data"]) for row in db.query(
        "SELECT data FROM audit_events WHERE subject_id='order-1'")]
    assert [(e["action"], e["actor_id"]) for e in events] == [
        ("review_created", "system"), ("trader_approve", "a"), ("co_captain_approve", "b")]
