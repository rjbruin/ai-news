from sqlalchemy.exc import IntegrityError

from app.models import NewsItem, NewsItemTag, Tag, User
from app.tagging import engine, nb


def test_nb_scores_relevant_tag_higher(sample_tags, sample_items):
    llm_item = sample_items[0]  # the GPT/LLM item
    docs = [
        nb.TagDoc(t.id, t.name, t.keyword_list, t.explanation or "", [])
        for t in sample_tags
    ]
    text = f"{llm_item.title}\n{llm_item.summary_text}"
    scores = nb.score_item(text, docs)
    llm_tag = next(t for t in sample_tags if t.name == "LLMs")
    robo_tag = next(t for t in sample_tags if t.name == "Robotics")
    assert scores[llm_tag.id] > scores[robo_tag.id]


def test_classify_nb_only_applies_expected_tag(app, sample_tags, sample_items):
    app.config["TAGGING_MODE"] = "nb_only"
    app.config["NB_CONFIDENCE_THRESHOLD"] = 0.05
    item = sample_items[1]  # robotics item
    result = engine.classify(
        f"{item.title}\n{item.summary_text}", sample_tags
    )
    robo_tag = next(t for t in sample_tags if t.name == "Robotics")
    assert robo_tag.id in result
    assert result[robo_tag.id][1] == "nb"


def test_apply_to_item_persists_links(app, db, sample_tags, sample_items):
    app.config["TAGGING_MODE"] = "nb_only"
    app.config["NB_CONFIDENCE_THRESHOLD"] = 0.05
    item = sample_items[0]
    n = engine.apply_to_item(item, sample_tags)
    assert n >= 1
    assert item.status == "tagged"
    refetched = db.session.get(NewsItem, item.id)
    assert refetched.tag_links.count() >= 1


def test_preview_returns_matches(app, sample_items):
    app.config["NB_CONFIDENCE_THRESHOLD"] = 0.05
    events = list(engine.preview_iter("Robots", ["robot", "humanoid"], "Robot news"))
    matches = [e for e in events if e.get("type") == "match"]
    assert any("robot" in e["title"].lower() for e in matches)


def test_llm_only_mode_without_key_returns_empty(app, sample_tags, sample_items):
    app.config["TAGGING_MODE"] = "llm_only"
    app.config["OPENROUTER_API_KEY"] = ""
    item = sample_items[0]
    result = engine.classify(f"{item.title}\n{item.summary_text}", sample_tags)
    assert result == {}


# ─────────────────────── graduated (LLM-first) mode ───────────────────────

def test_duplicate_global_newsitemtag_rejected_by_partial_index(db, sample_tags, sample_items):
    item = sample_items[0]
    tag = sample_tags[0]
    db.session.add(NewsItemTag(news_item_id=item.id, tag_id=tag.id, user_id=None, method="llm"))
    db.session.commit()

    db.session.add(NewsItemTag(news_item_id=item.id, tag_id=tag.id, user_id=None, method="llm"))
    try:
        db.session.commit()
        assert False, "expected IntegrityError from the partial unique index"
    except IntegrityError:
        db.session.rollback()


def test_classifier_state_transitions_at_thresholds(db, sample_tags, sample_items):
    tag = sample_tags[0]
    extra_items = [
        NewsItem(dedup_hash=f"extra-{i}", title=f"Extra item {i}", url=f"http://x/extra{i}")
        for i in range(2)
    ]
    db.session.add_all(extra_items)
    db.session.commit()
    all_items = sample_items + extra_items

    assert engine.classifier_state(tag, threshold_1=2, threshold_2=4) == "llm_only"

    # 2 distinct labeled items -> at threshold_1 -> hybrid.
    for item in all_items[:2]:
        db.session.add(NewsItemTag(news_item_id=item.id, tag_id=tag.id, user_id=None, method="llm"))
    db.session.commit()
    assert engine.classifier_state(tag, threshold_1=2, threshold_2=4) == "hybrid"

    # 4 distinct labeled items total -> at threshold_2 -> classifier_only.
    for item in all_items[2:4]:
        db.session.add(NewsItemTag(news_item_id=item.id, tag_id=tag.id, user_id=None, method="llm"))
    db.session.commit()
    assert engine.classifier_state(tag, threshold_1=2, threshold_2=4) == "classifier_only"


def test_classifier_state_ignores_nb_labels_for_graduation(db, sample_tags, sample_items):
    tag = sample_tags[0]
    for item in sample_items:
        db.session.add(NewsItemTag(news_item_id=item.id, tag_id=tag.id, user_id=None, method="nb"))
    db.session.commit()
    # Only "nb"-method rows exist — none count toward graduation.
    assert engine.classifier_state(tag, threshold_1=1, threshold_2=100) == "llm_only"


def test_topic_stats_reports_item_count(db, sample_tags, sample_items):
    tag = sample_tags[0]
    db.session.add(NewsItemTag(news_item_id=sample_items[0].id, tag_id=tag.id, user_id=None, method="llm"))
    db.session.commit()
    stats = engine.topic_stats([tag])
    assert stats[tag.id]["item_count"] == 1


def test_apply_to_item_graduated_llm_only_topic_calls_llm(app, db, sample_tags, sample_items, monkeypatch):
    app.config["TAGGING_MODE"] = "graduated"
    calls = []

    def fake_score_item(text, tags, **kw):
        calls.append([t["name"] for t in tags])
        return {sample_tags[0].name: 0.9}

    monkeypatch.setattr("app.tagging.engine.llm.score_item", fake_score_item)
    item = sample_items[0]
    n = engine.apply_to_item(item, sample_tags)
    assert n == 1
    assert len(calls) == 1
    assert sample_tags[0].name in calls[0]


def test_apply_to_item_excludes_classifier_only_topic_from_llm_call(app, db, sample_tags, sample_items, monkeypatch):
    app.config["TAGGING_MODE"] = "graduated"
    app.config["TOPIC_GRADUATION_THRESHOLD_1"] = 1
    app.config["TOPIC_GRADUATION_THRESHOLD_2"] = 1
    graduated_tag, other_tag = sample_tags
    other_item = sample_items[1]
    # Give graduated_tag 1 trustworthy label -> at/above threshold_2 (=1) -> classifier_only.
    db.session.add(NewsItemTag(news_item_id=other_item.id, tag_id=graduated_tag.id, user_id=None, method="llm"))
    db.session.commit()

    calls = []

    def fake_score_item(text, tags, **kw):
        calls.append([t["name"] for t in tags])
        return {}

    monkeypatch.setattr("app.tagging.engine.llm.score_item", fake_score_item)
    item = sample_items[0]
    engine.apply_to_item(item, sample_tags)
    # graduated_tag must never appear in the LLM call's taxonomy.
    for call_names in calls:
        assert graduated_tag.name not in call_names


def test_apply_to_item_llm_taxonomy_spans_global_and_private_topics(app, db, user, admin, sample_items, monkeypatch):
    app.config["TAGGING_MODE"] = "graduated"
    global_tag = Tag(name="GlobalTopic", scope="global")
    user_a_tag = Tag(name="UserATopic", scope="user", owner_user_id=user.id)
    user_b_tag = Tag(name="UserBTopic", scope="user", owner_user_id=admin.id)
    db.session.add_all([global_tag, user_a_tag, user_b_tag])
    db.session.commit()

    calls = []

    def fake_score_item(text, tags, **kw):
        calls.append([t["name"] for t in tags])
        return {"GlobalTopic": 0.9, "UserATopic": 0.8, "UserBTopic": 0.7}

    monkeypatch.setattr("app.tagging.engine.llm.score_item", fake_score_item)
    item = sample_items[0]
    n = engine.apply_to_item(item, [global_tag, user_a_tag, user_b_tag])

    assert n == 3
    assert len(calls) == 1  # exactly one LLM call for the whole item
    assert set(calls[0]) == {"GlobalTopic", "UserATopic", "UserBTopic"}

    global_link = NewsItemTag.query.filter_by(news_item_id=item.id, tag_id=global_tag.id).first()
    a_link = NewsItemTag.query.filter_by(news_item_id=item.id, tag_id=user_a_tag.id).first()
    b_link = NewsItemTag.query.filter_by(news_item_id=item.id, tag_id=user_b_tag.id).first()
    assert global_link.user_id is None
    assert a_link.user_id == user.id
    assert b_link.user_id == admin.id


def test_tag_docs_excludes_nb_method_examples(db, sample_tags, sample_items):
    tag = sample_tags[0]
    db.session.add(NewsItemTag(news_item_id=sample_items[0].id, tag_id=tag.id, user_id=None, method="nb"))
    db.session.commit()
    docs = engine._tag_docs([tag])
    assert docs[0].examples == []

    db.session.add(NewsItemTag(news_item_id=sample_items[1].id, tag_id=tag.id, user_id=None, method="llm"))
    db.session.commit()
    docs = engine._tag_docs([tag])
    assert len(docs[0].examples) == 1


def test_tag_docs_skips_orphaned_links_without_crashing(db, sample_tags, sample_items):
    """A NewsItemTag row whose news_item_id no longer matches any NewsItem
    (the item was deleted without its tag links being cleaned up) must be
    skipped, not crash _item_text() on a None item."""
    tag = sample_tags[0]
    db.session.add(NewsItemTag(news_item_id=999999, tag_id=tag.id, user_id=None, method="llm"))
    db.session.add(NewsItemTag(news_item_id=sample_items[0].id, tag_id=tag.id, user_id=None, method="llm"))
    db.session.commit()

    docs = engine._tag_docs([tag])
    assert len(docs[0].examples) == 1


def test_retag_all_covers_private_topics(app, db, user, sample_items, monkeypatch):
    from app.services import ingest

    app.config["TAGGING_MODE"] = "graduated"
    private_tag = Tag(name="PrivateOnly", scope="user", owner_user_id=user.id)
    db.session.add(private_tag)
    db.session.commit()

    monkeypatch.setattr(
        "app.tagging.engine.llm.score_item",
        lambda text, tags, **kw: {"PrivateOnly": 0.9},
    )
    ingest.retag_all()

    links = NewsItemTag.query.filter_by(tag_id=private_tag.id).all()
    assert len(links) == len(sample_items)
    assert all(link.user_id == user.id for link in links)
