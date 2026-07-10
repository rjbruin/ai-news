from app.models import AdminSettings, Summary, SummaryRun, User
from app.services import podcast


def _fake_tts_response(text_len_by_call):
    """Stand-in for httpx.post to ElevenLabs — returns minimal valid MP3-ish
    bytes without hitting the network, recording each call's text length."""
    def _post(url, *, headers, json, timeout):
        text_len_by_call.append(len(json["text"]))

        class _Resp:
            content = b"\xff\xfb\x90\x00" + b"\x00" * 100  # fake MPEG frame bytes

            def raise_for_status(self):
                pass

        return _Resp()

    return _post


def test_generate_audio_stream_computes_cost_from_characters(app, db, monkeypatch):
    settings = AdminSettings.get()
    settings.elevenlabs_voice_host_a = "voice-a"
    settings.elevenlabs_voice_host_b = "voice-b"
    db.session.commit()

    with app.app_context():
        app.config["ELEVENLABS_API_KEY"] = "sk-el-test"
        calls = []
        monkeypatch.setattr(podcast.httpx, "post", _fake_tts_response(calls))

        script = "HOST A: Hello there.\nHOST B: Hi, good to be here."
        events = list(podcast.generate_audio_stream(script))

    done = next(e for e in events if e[0] == "done")
    _, filename, chapters, cost = done
    assert filename.startswith("podcast_")

    total_chars = sum(calls)
    assert total_chars == len("Hello there.") + len("Hi, good to be here.")
    expected_cost = total_chars * podcast.ELEVENLABS_COST_PER_CHARACTER
    assert cost == expected_cost
    assert cost > 0


def test_run_podcast_job_persists_podcast_cost(app, db, monkeypatch):
    user = User(username="pod", email="pod@example.com", email_verified=True, podcast_enabled=True)
    user.set_password("password123")
    db.session.add(user)
    db.session.commit()

    summary = Summary(
        user_id=user.id, name="Daily", type_key="agentic_page",
        scope_mode="fixed_period", period="day", params={},
    )
    db.session.add(summary)
    db.session.commit()
    run = SummaryRun(summary_id=summary.id, news_podcast_script="HOST A: Hi.")
    db.session.add(run)
    db.session.commit()

    def _fake_stream(script):
        yield ("progress", 1, 1)
        yield ("done", "podcast_123.mp3", [{"title": "Intro", "start": 0}], 0.0042)

    monkeypatch.setattr(podcast, "generate_audio_stream", _fake_stream)

    from app.services.podcast_registry import PodcastJob

    job = PodcastJob(run_id=run.id, kind="audio")

    with app.test_request_context():
        podcast.run_podcast_job(app, job, run.id, user.id)

    db.session.refresh(run)
    assert run.news_podcast_audio == "podcast_123.mp3"
    assert run.podcast_cost == 0.0042


def test_edition_page_shows_cost_box_and_hides_old_headline_badge(auth_client, db, user):
    summary = Summary(
        user_id=user.id, name="Daily", type_key="agentic_page",
        scope_mode="fixed_period", period="day", params={},
    )
    db.session.add(summary)
    db.session.commit()
    run = SummaryRun(
        summary_id=summary.id, document=[{"type": "intro", "markdown": "hi"}],
        agent_cost=0.0123, podcast_cost=0.0042,
    )
    db.session.add(run)
    db.session.commit()

    resp = auth_client.get(f"/summaries/{summary.id}/editions/{run.id}")
    html = resp.data.decode()
    assert "Edition generation" in html
    assert "Podcast audio" in html
    assert "$0.0123" in html
    assert "$0.0042" in html
    assert '<span class="pill pill-amber" title="Generation cost">' not in html


def test_cost_box_sums_costs_across_revision_chain(auth_client, db, user):
    summary = Summary(
        user_id=user.id, name="Daily", type_key="agentic_page",
        scope_mode="fixed_period", period="day", params={},
    )
    db.session.add(summary)
    db.session.commit()
    root = SummaryRun(
        summary_id=summary.id, document=[{"type": "intro", "markdown": "hi"}],
        revision=1, agent_cost=0.0100, podcast_cost=0.0010,
    )
    db.session.add(root)
    db.session.commit()
    rev2 = SummaryRun(
        summary_id=summary.id, document=[{"type": "intro", "markdown": "hi v2"}],
        revision=2, parent_run_id=root.id, agent_cost=0.0050,
    )
    db.session.add(rev2)
    db.session.commit()

    # Viewing the LATEST revision should show the TOTAL across the whole chain.
    resp = auth_client.get(f"/summaries/{summary.id}/editions/{rev2.id}")
    html = resp.data.decode()
    assert "$0.0150" in html  # 0.0100 + 0.0050
    assert "$0.0010" in html  # podcast cost only on root
    assert "all 2 revisions" in html

    # Viewing the EARLIER revision should show the same chain total, not just its own cost.
    resp = auth_client.get(f"/summaries/{summary.id}/editions/{root.id}")
    html = resp.data.decode()
    assert "$0.0150" in html


def test_cost_box_omits_revision_count_for_single_revision(auth_client, db, user):
    summary = Summary(
        user_id=user.id, name="Daily", type_key="agentic_page",
        scope_mode="fixed_period", period="day", params={},
    )
    db.session.add(summary)
    db.session.commit()
    run = SummaryRun(
        summary_id=summary.id, document=[{"type": "intro", "markdown": "hi"}],
        revision=1, agent_cost=0.02,
    )
    db.session.add(run)
    db.session.commit()

    resp = auth_client.get(f"/summaries/{summary.id}/editions/{run.id}")
    html = resp.data.decode()
    assert "(all" not in html
    assert "revisions)" not in html


def test_editions_list_shows_podcast_cost_badge_on_icon(auth_client, db, user):
    user.podcast_enabled = True
    db.session.commit()
    summary = Summary(
        user_id=user.id, name="Daily", type_key="agentic_page",
        scope_mode="fixed_period", period="day", params={},
    )
    db.session.add(summary)
    db.session.commit()
    run = SummaryRun(
        summary_id=summary.id, news_podcast_audio="podcast_1.mp3", podcast_cost=0.0099,
    )
    db.session.add(run)
    db.session.commit()

    resp = auth_client.get("/summaries")
    assert b"$0.0099" in resp.data
