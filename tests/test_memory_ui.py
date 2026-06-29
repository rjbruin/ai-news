from app.agent import memory
from app.models import Summary


def _agentic_summary(db, user):
    s = Summary(
        user_id=user.id, name="Mem Daily", type_key="agentic_page",
        scope_mode="fixed_period", period="day", params={},
    )
    db.session.add(s)
    db.session.commit()
    return s


def test_memory_page_seeds_defaults(auth_client, db, user):
    s = _agentic_summary(db, user)
    resp = auth_client.get(f"/summaries/{s.id}/memory")
    assert resp.status_code == 200
    assert b"INTERESTS.md" in resp.data
    assert b"Content configuration" in resp.data
    # defaults seeded so the textareas have content
    assert memory.read(user, s, "interests")
    assert memory.read(user, s, "content_config")


def test_memory_page_saves_edits(auth_client, db, user):
    s = _agentic_summary(db, user)
    resp = auth_client.post(
        f"/summaries/{s.id}/memory",
        data={
            "mem_interests": "Only robotics, please.",
            "mem_content_config": "One section, five items.",
            "mem_history": "Started fresh.",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert memory.read(user, s, "interests") == "Only robotics, please."
    assert memory.read(user, s, "content_config") == "One section, five items."
    assert memory.read(user, s, "history") == "Started fresh."


def test_memory_page_ownership(auth_client, db, admin):
    other = Summary(user_id=admin.id, name="Theirs", type_key="agentic_page",
                    scope_mode="fixed_period", period="day", params={})
    db.session.add(other)
    db.session.commit()
    assert auth_client.get(f"/summaries/{other.id}/memory").status_code == 403


def test_interests_shared_across_summaries(auth_client, db, user):
    s1 = _agentic_summary(db, user)
    s2 = _agentic_summary(db, user)
    auth_client.post(
        f"/summaries/{s1.id}/memory",
        data={"mem_interests": "shared interests", "mem_content_config": "x", "mem_history": ""},
        follow_redirects=True,
    )
    # interests is user-level, so s2 sees the same value
    assert memory.read(user, s2, "interests") == "shared interests"
