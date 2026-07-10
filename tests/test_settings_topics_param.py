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


def _tag_id_in_tier_box(html: bytes, tier: str, tag) -> bool:
    # The hidden inputs mirroring tier membership are only populated
    # client-side (see app/static/js/tier_picker.js), so a server-rendered
    # response has no JS to run — check the badge lands in the right
    # server-rendered tier box instead, by isolating that box's markup
    # between its `data-tier="..."` marker and the next one.
    import re

    text = html.decode()
    positions = [(m.start(), m.group(1)) for m in re.finditer(r'data-tier="(\w+)"', text)]
    for i, (pos, name) in enumerate(positions):
        if name == tier:
            end = positions[i + 1][0] if i + 1 < len(positions) else len(text)
            return f'data-tag-id="{tag.id}"' in text[pos:end]
    return False


def test_settings_saves_topic_tiers(auth_client, db, user):
    summary = _agentic_summary(db, user)
    tag_a = Tag(name="Highlighted Topic", scope="global")
    tag_b = Tag(name="Suppressed Topic", scope="global")
    db.session.add_all([tag_a, tag_b])
    db.session.commit()

    resp = auth_client.post(
        "/settings",
        data={
            "param_topic_tiers_highlights": [str(tag_a.id)],
            "param_topic_tiers_none": [str(tag_b.id)],
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    db.session.refresh(summary)
    tiers = summary.params.get("topic_tiers")
    assert tiers["highlights"] == [tag_a.id]
    assert tiers["none"] == [tag_b.id]


def test_settings_page_defaults_new_topics_to_complete(auth_client, db, user):
    _agentic_summary(db, user)
    tag = Tag(name="Never Configured", scope="global")
    db.session.add(tag)
    db.session.commit()

    resp = auth_client.get("/settings")
    assert resp.status_code == 200
    assert _tag_id_in_tier_box(resp.data, "complete", tag)


def test_settings_page_prepopulates_saved_tiers(auth_client, db, user):
    summary = _agentic_summary(db, user)
    highlight_tag = Tag(name="Highlight Me", scope="global")
    none_tag = Tag(name="Skip Me", scope="global")
    db.session.add_all([highlight_tag, none_tag])
    db.session.commit()
    summary.params = {"topic_tiers": {"highlights": [highlight_tag.id], "none": [none_tag.id]}}
    db.session.commit()

    resp = auth_client.get("/settings")
    assert resp.status_code == 200
    assert _tag_id_in_tier_box(resp.data, "highlights", highlight_tag)
    assert _tag_id_in_tier_box(resp.data, "none", none_tag)


def test_prompt_includes_topic_emphasis_section_when_set(app, db, user):
    summary = _agentic_summary(db, user)
    highlight_tag = Tag(name="Highlight Tag", scope="global")
    none_tag = Tag(name="Suppressed Tag", scope="global")
    db.session.add_all([highlight_tag, none_tag])
    db.session.commit()
    summary.params = {"topic_tiers": {"highlights": [highlight_tag.id], "none": [none_tag.id]}}
    db.session.commit()

    with app.app_context():
        prompt = compose_system_prompt(user, summary)
    assert "TOPIC EMPHASIS" in prompt
    assert "Highlight Tag" in prompt
    assert "Suppressed Tag" in prompt


def test_prompt_omits_topic_emphasis_section_when_unset(app, db, user):
    summary = _agentic_summary(db, user)
    with app.app_context():
        prompt = compose_system_prompt(user, summary)
    assert "TOPIC EMPHASIS" not in prompt
