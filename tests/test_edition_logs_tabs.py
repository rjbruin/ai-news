from app.models import Summary, SummaryRun


def test_logs_page_shows_no_tabs_for_single_revision(auth_client, db, user):
    summary = Summary(
        user_id=user.id, name="Daily", type_key="agentic_page",
        scope_mode="fixed_period", period="day", params={},
    )
    db.session.add(summary)
    db.session.commit()
    run = SummaryRun(summary_id=summary.id, revision=1, agent_log=[{"type": "start"}])
    db.session.add(run)
    db.session.commit()

    resp = auth_client.get(f"/summaries/{summary.id}/editions/{run.id}/logs")
    assert resp.status_code == 200
    assert b"nav-tabs" not in resp.data


def test_logs_page_shows_a_tab_per_revision(auth_client, db, user):
    summary = Summary(
        user_id=user.id, name="Daily", type_key="agentic_page",
        scope_mode="fixed_period", period="day", params={},
    )
    db.session.add(summary)
    db.session.commit()
    root = SummaryRun(summary_id=summary.id, revision=1, agent_log=[{"type": "start"}])
    db.session.add(root)
    db.session.commit()
    rev2 = SummaryRun(
        summary_id=summary.id, revision=2, parent_run_id=root.id,
        agent_log=[{"type": "start"}],
    )
    db.session.add(rev2)
    db.session.commit()

    resp = auth_client.get(f"/summaries/{summary.id}/editions/{root.id}/logs")
    html = resp.data.decode()
    assert "nav-tabs" in html
    assert "rev 1" in html
    assert "rev 2" in html

    resp2 = auth_client.get(f"/summaries/{summary.id}/editions/{rev2.id}/logs")
    html2 = resp2.data.decode()
    assert "nav-tabs" in html2
    assert "rev 1" in html2
    assert "rev 2" in html2


def test_logs_tab_active_state_matches_current_revision(auth_client, db, user):
    import re

    summary = Summary(
        user_id=user.id, name="Daily", type_key="agentic_page",
        scope_mode="fixed_period", period="day", params={},
    )
    db.session.add(summary)
    db.session.commit()
    root = SummaryRun(summary_id=summary.id, revision=1)
    db.session.add(root)
    db.session.commit()
    rev2 = SummaryRun(summary_id=summary.id, revision=2, parent_run_id=root.id)
    db.session.add(rev2)
    db.session.commit()

    resp = auth_client.get(f"/summaries/{summary.id}/editions/{rev2.id}/logs")
    html = resp.data.decode()

    def _classes_for(run_id):
        pattern = rf'<a class="([^"]*)"\s*\n?\s*href="/summaries/{summary.id}/editions/{run_id}/logs">'
        m = re.search(pattern, html)
        return m.group(1) if m else ""

    assert "active" in _classes_for(rev2.id)
    assert "active" not in _classes_for(root.id)
