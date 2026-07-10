from app.models import NewsItem, NewsItemTag, Tag


def test_admin_can_create_global_topic(admin_client, db):
    resp = admin_client.post(
        "/topics/create",
        data={"name": "New Topic", "description": "A one-liner.", "is_global": "1"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    tag = Tag.query.filter_by(name="New Topic").first()
    assert tag is not None
    assert tag.scope == "global"
    assert tag.owner_user_id is None
    assert tag.explanation == "A one-liner."


def test_approved_user_can_create_private_topic(auth_client, db, user):
    user.approved = True
    db.session.commit()

    resp = auth_client.post(
        "/topics/create",
        data={"name": "My Private Topic", "description": "Mine only."},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    tag = Tag.query.filter_by(name="My Private Topic").first()
    assert tag.scope == "user"
    assert tag.owner_user_id == user.id


def test_non_admin_is_global_flag_is_ignored(auth_client, db, user):
    user.approved = True
    db.session.commit()

    auth_client.post(
        "/topics/create",
        data={"name": "Spoofed Global", "description": "x", "is_global": "1"},
    )
    tag = Tag.query.filter_by(name="Spoofed Global").first()
    assert tag.scope == "user"
    assert tag.owner_user_id == user.id


def test_unapproved_user_cannot_create_topic(auth_client, db):
    resp = auth_client.post("/topics/create", data={"name": "x", "description": "y"})
    assert resp.status_code == 403


def test_unapproved_user_can_view_topics_list(auth_client):
    assert auth_client.get("/topics").status_code == 200


def test_duplicate_topic_name_rejected_on_create(admin_client, db):
    db.session.add(Tag(name="Existing", scope="global"))
    db.session.commit()

    resp = admin_client.post(
        "/topics/create", data={"name": "Existing", "description": "x"}, follow_redirects=True,
    )
    assert resp.status_code == 200
    assert Tag.query.filter_by(name="Existing").count() == 1


def test_admin_can_edit_any_topic(admin_client, db, user):
    tag = Tag(name="Old Name", scope="user", owner_user_id=user.id)
    db.session.add(tag)
    db.session.commit()

    resp = admin_client.post(
        f"/topics/{tag.id}/edit", data={"name": "New Name", "description": "updated"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    db.session.refresh(tag)
    assert tag.name == "New Name"
    assert tag.explanation == "updated"


def test_user_cannot_edit_another_users_private_topic(auth_client, db, admin):
    admin_tag = Tag(name="Admin's Topic", scope="user", owner_user_id=admin.id)
    db.session.add(admin_tag)
    db.session.commit()

    resp = auth_client.post(
        f"/topics/{admin_tag.id}/edit", data={"name": "Hijacked", "description": ""},
    )
    assert resp.status_code == 403


def test_user_cannot_edit_global_topic(auth_client, db, user):
    user.approved = True
    global_tag = Tag(name="Global One", scope="global")
    db.session.add(global_tag)
    db.session.commit()

    resp = auth_client.post(
        f"/topics/{global_tag.id}/edit", data={"name": "Hijacked", "description": ""},
    )
    assert resp.status_code == 403


def test_admin_can_set_classifier_mode_override(admin_client, db):
    tag = Tag(name="Overridable", scope="global")
    db.session.add(tag)
    db.session.commit()

    resp = admin_client.post(
        f"/topics/{tag.id}/edit",
        data={"name": "Overridable", "description": "", "classifier_mode": "classifier_only"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    db.session.refresh(tag)
    assert tag.classifier_mode == "classifier_only"


def test_admin_can_clear_classifier_mode_override(admin_client, db):
    tag = Tag(name="WasOverridden", scope="global", classifier_mode="hybrid")
    db.session.add(tag)
    db.session.commit()

    admin_client.post(
        f"/topics/{tag.id}/edit",
        data={"name": "WasOverridden", "description": "", "classifier_mode": ""},
    )
    db.session.refresh(tag)
    assert tag.classifier_mode is None


def test_invalid_classifier_mode_is_ignored(admin_client, db):
    tag = Tag(name="Invalid Mode", scope="global")
    db.session.add(tag)
    db.session.commit()

    admin_client.post(
        f"/topics/{tag.id}/edit",
        data={"name": "Invalid Mode", "description": "", "classifier_mode": "not_a_real_mode"},
    )
    db.session.refresh(tag)
    assert tag.classifier_mode is None


def test_non_admin_classifier_mode_is_ignored(auth_client, db, user):
    user.approved = True
    tag = Tag(name="Mine", scope="user", owner_user_id=user.id)
    db.session.add(tag)
    db.session.commit()

    auth_client.post(
        f"/topics/{tag.id}/edit",
        data={"name": "Mine", "description": "", "classifier_mode": "classifier_only"},
    )
    db.session.refresh(tag)
    assert tag.classifier_mode is None


def test_rename_to_existing_name_rejected(admin_client, db):
    a = Tag(name="Topic A", scope="global")
    b = Tag(name="Topic B", scope="global")
    db.session.add_all([a, b])
    db.session.commit()

    resp = admin_client.post(
        f"/topics/{b.id}/edit", data={"name": "Topic A", "description": ""}, follow_redirects=True,
    )
    assert resp.status_code == 200
    db.session.refresh(b)
    assert b.name == "Topic B"  # unchanged


def test_archive_and_restore_preserve_historical_labels(admin_client, db, user):
    tag = Tag(name="Archivable", scope="global")
    db.session.add(tag)
    db.session.commit()
    item = NewsItem(dedup_hash="h-archive-1", title="An item", url="http://x/archive1")
    db.session.add(item)
    db.session.commit()
    db.session.add(NewsItemTag(news_item_id=item.id, tag_id=tag.id, user_id=None, method="llm"))
    db.session.commit()

    resp = admin_client.post(f"/topics/{tag.id}/archive", follow_redirects=True)
    assert resp.status_code == 200
    db.session.refresh(tag)
    assert tag.archived_at is not None
    assert NewsItemTag.query.filter_by(tag_id=tag.id).count() == 1  # label preserved

    resp = admin_client.post(f"/topics/{tag.id}/restore", follow_redirects=True)
    assert resp.status_code == 200
    db.session.refresh(tag)
    assert tag.archived_at is None
    assert NewsItemTag.query.filter_by(tag_id=tag.id).count() == 1


def test_archived_topic_excluded_from_default_ingest_tag_set(db):
    from app.models import utcnow

    active = Tag(name="Active One", scope="global")
    archived = Tag(name="Archived One", scope="global")
    db.session.add_all([active, archived])
    db.session.commit()
    archived.archived_at = utcnow()
    db.session.commit()

    tags = Tag.query.filter_by(archived_at=None).all()
    names = {t.name for t in tags}
    assert "Active One" in names
    assert "Archived One" not in names


def test_tags_redirects_to_topics(client):
    resp = client.get("/tags")
    assert resp.status_code == 301
    assert resp.headers["Location"].endswith("/topics")
