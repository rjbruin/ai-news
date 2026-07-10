from app.models import NewsItem, NewsItemTag, Tag


def _item(hash_suffix, title):
    return NewsItem(dedup_hash=f"news-filter-{hash_suffix}", title=title, url=f"http://x/{hash_suffix}")


def test_filter_by_global_topic(auth_client, db):
    tag = Tag(name="Filterable", scope="global")
    matching = _item("1", "Matches")
    other = _item("2", "Does not match")
    db.session.add_all([tag, matching, other])
    db.session.commit()
    db.session.add(NewsItemTag(news_item_id=matching.id, tag_id=tag.id, user_id=None, method="llm"))
    db.session.commit()

    resp = auth_client.get(f"/news?topic={tag.id}")
    assert resp.status_code == 200
    assert b"Matches" in resp.data
    assert b"Does not match" not in resp.data


def test_filter_by_another_users_private_topic_returns_nothing(auth_client, db, user, admin):
    private_tag = Tag(name="Admins Private", scope="user", owner_user_id=admin.id)
    item = _item("3", "Admin only item")
    db.session.add_all([private_tag, item])
    db.session.commit()
    db.session.add(NewsItemTag(news_item_id=item.id, tag_id=private_tag.id, user_id=admin.id, method="llm"))
    db.session.commit()

    # user (not admin) crafts the URL manually with admin's private tag id.
    resp = auth_client.get(f"/news?topic={private_tag.id}")
    assert resp.status_code == 200
    assert b"Admin only item" not in resp.data


def test_filter_by_own_private_topic_returns_matches(auth_client, db, user):
    private_tag = Tag(name="My Private", scope="user", owner_user_id=user.id)
    item = _item("4", "My private item")
    db.session.add_all([private_tag, item])
    db.session.commit()
    db.session.add(NewsItemTag(news_item_id=item.id, tag_id=private_tag.id, user_id=user.id, method="llm"))
    db.session.commit()

    resp = auth_client.get(f"/news?topic={private_tag.id}")
    assert resp.status_code == 200
    assert b"My private item" in resp.data


def test_multi_topic_filter_uses_or_semantics(auth_client, db):
    tag_a = Tag(name="TopicA", scope="global")
    tag_b = Tag(name="TopicB", scope="global")
    item_a = _item("5", "Has A")
    item_b = _item("6", "Has B")
    db.session.add_all([tag_a, tag_b, item_a, item_b])
    db.session.commit()
    db.session.add(NewsItemTag(news_item_id=item_a.id, tag_id=tag_a.id, user_id=None, method="llm"))
    db.session.add(NewsItemTag(news_item_id=item_b.id, tag_id=tag_b.id, user_id=None, method="llm"))
    db.session.commit()

    resp = auth_client.get(f"/news?topic={tag_a.id}&topic={tag_b.id}")
    assert resp.status_code == 200
    assert b"Has A" in resp.data
    assert b"Has B" in resp.data


def test_no_filter_shows_all_recent_items(auth_client, db, sample_items):
    resp = auth_client.get("/news")
    assert resp.status_code == 200
    for item in sample_items:
        assert item.title.encode() in resp.data


def test_item_badge_hides_other_users_private_topic(auth_client, db, user, admin):
    private_tag = Tag(name="SecretAdminTopic", scope="user", owner_user_id=admin.id)
    item = _item("7", "Shared item with a private tag")
    db.session.add_all([private_tag, item])
    db.session.commit()
    db.session.add(NewsItemTag(news_item_id=item.id, tag_id=private_tag.id, user_id=admin.id, method="llm"))
    db.session.commit()

    # user (not admin) sees the item on the unfiltered News page, but must
    # NOT see the badge for admin's private topic.
    resp = auth_client.get("/news")
    assert b"Shared item with a private tag" in resp.data
    assert b"SecretAdminTopic" not in resp.data


def test_item_badge_shows_own_private_topic(auth_client, db, user):
    private_tag = Tag(name="MyOwnVisibleTopic", scope="user", owner_user_id=user.id)
    item = _item("8", "Item with my own tag")
    db.session.add_all([private_tag, item])
    db.session.commit()
    db.session.add(NewsItemTag(news_item_id=item.id, tag_id=private_tag.id, user_id=user.id, method="llm"))
    db.session.commit()

    resp = auth_client.get("/news")
    assert b"MyOwnVisibleTopic" in resp.data
