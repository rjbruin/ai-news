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


def test_edition_page_shows_pdf_card_for_follower(auth_client, db, user, admin):
    """The "Also available as" card was previously gated to the Dispatch
    owner, hiding the PDF (and podcast) links from anyone just following
    someone else's published Dispatch."""
    dispatch = _own_dispatch(db, admin, pdf_export_enabled=True)
    run = _run(db, dispatch, pdf_file="edition_1.pdf")
    user.follow(dispatch)
    db.session.commit()

    html = auth_client.get(f"/summaries/{dispatch.id}/editions/{run.id}").data.decode()
    assert "Also available as:" in html
    assert "channel-pill--pdf" in html


def test_edition_page_hides_pdf_card_when_export_disabled_for_follower(auth_client, db, user, admin):
    dispatch = _own_dispatch(db, admin, pdf_export_enabled=False)
    run = _run(db, dispatch, pdf_file="edition_1.pdf")
    user.follow(dispatch)
    db.session.commit()

    html = auth_client.get(f"/summaries/{dispatch.id}/editions/{run.id}").data.decode()
    assert "Also available as:" not in html


# ── Automatic PDF generation on edition creation ────────────────────────────

def test_autogenerate_pdf_when_export_enabled(app, db, user, monkeypatch):
    """Weasyprint needs native libs (Pango/cairo) unavailable in the test
    env, so this stubs generate_and_store_pdf and only asserts the wiring:
    it gets called with the right run when pdf_export_enabled is on."""
    from app.services import summarize

    dispatch = _own_dispatch(db, user, pdf_export_enabled=True)
    run = _run(db, dispatch)

    calls = []

    def fake_generate(summary, run_arg, *, font_scale=None, base_url="/"):
        calls.append((summary.id, run_arg.id))
        run_arg.pdf_file = f"edition_{run_arg.id}.pdf"
        db.session.commit()
        return b"%PDF-fake"

    monkeypatch.setattr("app.services.pdf_export.generate_and_store_pdf", fake_generate)

    summarize._maybe_autogenerate_pdf(dispatch, run)

    assert calls == [(dispatch.id, run.id)]
    db.session.refresh(run)
    assert run.pdf_file == f"edition_{run.id}.pdf"


def test_autogenerate_pdf_skips_when_export_disabled(app, db, user, monkeypatch):
    from app.services import summarize

    dispatch = _own_dispatch(db, user, pdf_export_enabled=False)
    run = _run(db, dispatch)

    calls = []
    monkeypatch.setattr(
        "app.services.pdf_export.generate_and_store_pdf",
        lambda *a, **kw: calls.append(1),
    )

    summarize._maybe_autogenerate_pdf(dispatch, run)

    assert calls == []
    assert run.pdf_file is None


def test_autogenerate_pdf_skips_if_already_generated(app, db, user, monkeypatch):
    from app.services import summarize

    dispatch = _own_dispatch(db, user, pdf_export_enabled=True)
    run = _run(db, dispatch, pdf_file="edition_already.pdf")

    calls = []
    monkeypatch.setattr(
        "app.services.pdf_export.generate_and_store_pdf",
        lambda *a, **kw: calls.append(1),
    )

    summarize._maybe_autogenerate_pdf(dispatch, run)

    assert calls == []


# ── autogenerate_channels wired into on-demand generation routes ───────────
# (previously only the scheduled ingest_all_due loop called the
# podcast/PDF auto-generation hooks — a manually-triggered edition, e.g. via
# "New custom edition", silently skipped them.)

def test_summary_open_triggers_autogenerate_channels(auth_client, db, user, monkeypatch):
    from app.services import summarize

    dispatch = _own_dispatch(db, user, pdf_export_enabled=True)
    run = _run(db, dispatch)

    calls = []
    monkeypatch.setattr(summarize, "build_summary", lambda *a, **kw: (None, [], run))
    monkeypatch.setattr(summarize, "autogenerate_channels", lambda s, r: calls.append((s.id, r.id)))

    resp = auth_client.get(f"/summaries/{dispatch.id}/open")
    assert resp.status_code == 302
    assert calls == [(dispatch.id, run.id)]


def test_summary_generate_triggers_autogenerate_channels(auth_client, db, user, monkeypatch):
    from app.services import summarize

    dispatch = _own_dispatch(db, user, pdf_export_enabled=True)
    run = _run(db, dispatch)

    calls = []
    monkeypatch.setattr(summarize, "build_summary", lambda *a, **kw: (None, [], run))
    monkeypatch.setattr(summarize, "autogenerate_channels", lambda s, r: calls.append((s.id, r.id)))

    resp = auth_client.post(f"/summaries/{dispatch.id}/generate", follow_redirects=True)
    assert resp.status_code == 200
    assert calls == [(dispatch.id, run.id)]


def test_autogenerate_channels_skips_none_run(app, db):
    from app.services import summarize

    # Should not raise when a caller passes a None run (e.g. a cancelled job).
    summarize.autogenerate_channels(None, None)
