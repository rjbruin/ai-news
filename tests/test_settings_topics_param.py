from app.agent.prompt import compose_system_prompt
from app.models import Summary, Tag


def _agentic_summary(db, user):
    s = Summary(
        user_id=user.id, name="Topics Daily", type_key="agentic_page",
        scope_mode="fixed_period", period="day", params={},
    )
    db.session.add(s)
    db.session.commit()
    return s


def test_settings_saves_emphasized_topic_ids(auth_client, db, user):
    summary = _agentic_summary(db, user)
    tag_a = Tag(name="Robotics Focus", scope="global")
    tag_b = Tag(name="AI Safety Focus", scope="global")
    db.session.add_all([tag_a, tag_b])
    db.session.commit()

    resp = auth_client.post(
        "/settings",
        data={"param_emphasized_topic_ids": [str(tag_a.id), str(tag_b.id)]},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    db.session.refresh(summary)
    assert set(summary.params.get("emphasized_topic_ids")) == {tag_a.id, tag_b.id}


def test_settings_page_prepopulates_selected_topic_badges(auth_client, db, user):
    summary = _agentic_summary(db, user)
    tag = Tag(name="Preselected Topic", scope="global")
    db.session.add(tag)
    db.session.commit()
    summary.params = {"emphasized_topic_ids": [tag.id]}
    db.session.commit()

    resp = auth_client.get("/settings")
    assert resp.status_code == 200
    assert b"Preselected Topic" in resp.data


def test_prompt_includes_emphasized_topics_section_when_set(app, db, user):
    summary = _agentic_summary(db, user)
    tag = Tag(name="Emphasis Tag", scope="global")
    db.session.add(tag)
    db.session.commit()
    summary.params = {"emphasized_topic_ids": [tag.id]}
    db.session.commit()

    with app.app_context():
        prompt = compose_system_prompt(user, summary)
    assert "EMPHASIZED TOPICS" in prompt
    assert "Emphasis Tag" in prompt


def test_prompt_omits_emphasized_topics_section_when_unset(app, db, user):
    summary = _agentic_summary(db, user)
    with app.app_context():
        prompt = compose_system_prompt(user, summary)
    assert "EMPHASIZED TOPICS" not in prompt
