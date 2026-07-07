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


def test_old_memory_link_redirects_to_settings(auth_client, db, user):
    # Memory editing moved into Settings; old bookmarked /memory links still
    # work by redirecting there instead of 404ing.
    s = _agentic_summary(db, user)
    resp = auth_client.get(f"/summaries/{s.id}/memory")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/settings")


def test_settings_page_seeds_memory_defaults(auth_client, db, user):
    s = _agentic_summary(db, user)
    resp = auth_client.get("/settings")
    assert resp.status_code == 200
    assert b"Content configuration" in resp.data
    # defaults seeded so the textareas have content
    assert memory.read(user, s, "interests")
    assert memory.read(user, s, "content_config")


def test_settings_page_saves_memory_edits(auth_client, db, user):
    s = _agentic_summary(db, user)
    resp = auth_client.post(
        "/settings",
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
