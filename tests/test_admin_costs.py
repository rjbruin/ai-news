from datetime import timedelta

from app.models import ApiKey, ApiKeyUsage, Summary, SummaryRun, User, utcnow
from app.services import costs


def _global_openrouter_key(db):
    key = ApiKey.query.filter_by(is_global=True, provider="openrouter").first()
    if key is None:
        key = ApiKey(is_global=True, provider="openrouter", label="Global")
        db.session.add(key)
        db.session.commit()
    return key


def test_openrouter_daily_costs_only_counts_global_key(app, db):
    with app.app_context():
        global_key = _global_openrouter_key(db)
        personal_key = ApiKey(owner_user_id=None, label="Personal", provider="openrouter")
        db.session.add(personal_key)
        db.session.commit()

        db.session.add(ApiKeyUsage(api_key_id=global_key.id, kind="ingest", tokens=100, cost=0.5))
        db.session.add(ApiKeyUsage(api_key_id=personal_key.id, kind="ingest", tokens=100, cost=99.0))
        db.session.commit()

        series = costs.openrouter_daily_costs(days=7)
        today = utcnow().date().isoformat()
        today_entry = next(d for d in series if d["date"] == today)
        assert today_entry["cost"] == 0.5  # personal key's spend excluded


def test_openrouter_daily_costs_excludes_old_rows(app, db):
    with app.app_context():
        global_key = _global_openrouter_key(db)
        old = ApiKeyUsage(api_key_id=global_key.id, kind="ingest", tokens=10, cost=1.0)
        db.session.add(old)
        db.session.commit()
        old.created_at = utcnow() - timedelta(days=60)
        db.session.commit()

        series = costs.openrouter_daily_costs(days=30)
        assert sum(d["cost"] for d in series) == 0.0


def test_elevenlabs_daily_costs_sums_podcast_cost(app, db, user):
    with app.app_context():
        summary = Summary(
            user_id=user.id, name="Daily", type_key="agentic_page",
            scope_mode="fixed_period", period="day", params={},
        )
        db.session.add(summary)
        db.session.commit()
        db.session.add(SummaryRun(summary_id=summary.id, podcast_cost=0.01))
        db.session.add(SummaryRun(summary_id=summary.id, podcast_cost=0.02))
        db.session.add(SummaryRun(summary_id=summary.id, podcast_cost=None))
        db.session.commit()

        series = costs.elevenlabs_daily_costs(days=7)
        today = utcnow().date().isoformat()
        today_entry = next(d for d in series if d["date"] == today)
        assert abs(today_entry["cost"] - 0.03) < 1e-9


def test_openrouter_cost_by_kind_breaks_down_correctly(app, db):
    with app.app_context():
        global_key = _global_openrouter_key(db)
        db.session.add(ApiKeyUsage(api_key_id=global_key.id, kind="ingest", tokens=1, cost=1.0))
        db.session.add(ApiKeyUsage(api_key_id=global_key.id, kind="ingest", tokens=1, cost=2.0))
        db.session.add(ApiKeyUsage(api_key_id=global_key.id, kind="confirm", tokens=1, cost=0.5))
        db.session.commit()

        breakdown = {d["kind"]: d["cost"] for d in costs.openrouter_cost_by_kind(days=7)}
        assert breakdown["ingest"] == 3.0
        assert breakdown["confirm"] == 0.5


def test_cost_summary_shape(app, db):
    with app.app_context():
        summary = costs.cost_summary(days=14)
        assert summary["days"] == 14
        assert "openrouter_daily" in summary and len(summary["openrouter_daily"]) == 14
        assert "elevenlabs_daily" in summary and len(summary["elevenlabs_daily"]) == 14
        assert "openrouter_total" in summary
        assert "elevenlabs_total" in summary


def test_admin_page_renders_costs_section(admin_client, db):
    with_key = _global_openrouter_key(db)
    db.session.add(ApiKeyUsage(api_key_id=with_key.id, kind="ingest", tokens=10, cost=1.2345))
    db.session.commit()

    resp = admin_client.get("/admin/")
    assert resp.status_code == 200
    assert b"Costs" in resp.data
    assert b"cost-chart-openrouter" in resp.data
    assert b"cost-chart-elevenlabs" in resp.data
