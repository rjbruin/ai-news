from app.models import NewsItem
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
    matches = engine.preview("Robots", ["robot", "humanoid"], "Robot news")
    titles = [m["item"].title for m in matches]
    assert any("robot" in t.lower() for t in titles)


def test_llm_only_mode_without_key_returns_empty(app, sample_tags, sample_items):
    app.config["TAGGING_MODE"] = "llm_only"
    app.config["OPENROUTER_API_KEY"] = ""
    item = sample_items[0]
    result = engine.classify(f"{item.title}\n{item.summary_text}", sample_tags)
    assert result == {}
