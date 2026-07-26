from app.models import Summary, SummaryRun


def _own_dispatch(db, user, pdf_export_enabled=False):
    s = Summary(
        user_id=user.id, name="Daily", type_key="agentic_page",
        scope_mode="fixed_period", period="day", params={},
        pdf_export_enabled=pdf_export_enabled,
    )
    db.session.add(s)
    db.session.commit()
    user.follow(s)
    db.session.commit()
    return s


def _run(db, summary, pdf_file=None):
    r = SummaryRun(summary_id=summary.id, label="Monday", content="<p>hi</p>", status="ok", pdf_file=pdf_file)
    db.session.add(r)
    db.session.commit()
    return r


def test_settings_saves_description_and_pdf_export(auth_client, db, user):
    dispatch = _own_dispatch(db, user)
    resp = auth_client.post(
        "/dispatch/settings",
        data={"period": "day", "description": "My favorite AI news.", "pdf_export_enabled": "1"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    db.session.refresh(dispatch)
    assert dispatch.description == "My favorite AI news."
    assert dispatch.pdf_export_enabled is True


def test_settings_blank_description_saves_as_none(auth_client, db, user):
    dispatch = _own_dispatch(db, user)
    dispatch.description = "old"
    db.session.commit()
    auth_client.post("/dispatch/settings", data={"period": "day", "description": "   "})
    db.session.refresh(dispatch)
    assert dispatch.description is None


def test_pdf_export_disabled_blocks_owner_export(auth_client, db, user):
    dispatch = _own_dispatch(db, user, pdf_export_enabled=False)
    run = _run(db, dispatch)
    resp = auth_client.get(f"/summaries/{dispatch.id}/editions/{run.id}/export")
    assert resp.status_code == 404


def test_pdf_export_disabled_blocks_follower_download(auth_client, db, user, admin):
    dispatch = _own_dispatch(db, admin, pdf_export_enabled=False)
    run = _run(db, dispatch, pdf_file="edition_1.pdf")
    user.follow(dispatch)
    db.session.commit()

    resp = auth_client.get(f"/summaries/{dispatch.id}/editions/{run.id}/pdf")
    assert resp.status_code == 404


def test_pdf_export_enabled_allows_follower_download(auth_client, db, user, admin, tmp_path, app):
    app.instance_path = str(tmp_path)
    (tmp_path / "pdfs").mkdir()
    (tmp_path / "pdfs" / "edition_1.pdf").write_bytes(b"%PDF-1.4 fake")

    dispatch = _own_dispatch(db, admin, pdf_export_enabled=True)
    run = _run(db, dispatch, pdf_file="edition_1.pdf")
    user.follow(dispatch)
    db.session.commit()

    resp = auth_client.get(f"/summaries/{dispatch.id}/editions/{run.id}/pdf")
    assert resp.status_code == 200
