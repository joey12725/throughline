"""Pieces the Docker install relies on (docker-compose.yml, scripts/install.sh)."""

from conftest import ingest, message

from throughline import agent_view, embeddings, ops


def test_embed_until_idle_is_quiet_without_a_provider(monkeypatch):
    for name in ("PROVIDER", "BASE_URL", "MODEL", "DIMENSIONS"):
        monkeypatch.delenv(f"THROUGHLINE_EMBEDDING_{name}", raising=False)
    result = embeddings.run_until_idle(10)
    assert result["status"] == "unavailable" and result["embedded"] == 0


def test_embed_until_idle_stops_when_nothing_is_pending(client, channel, monkeypatch):
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_PROVIDER", "local")
    ingest(client, [message(i, content=f"hello number {i}") for i in range(3)])
    first = embeddings.run_until_idle(60)
    assert first["status"] == "idle" and first["embedded"] > 0
    assert embeddings.run_until_idle(60) == dict(status="idle", embedded=0)


def test_one_timezone_setting(monkeypatch):
    monkeypatch.delenv("THROUGHLINE_TIMEZONE", raising=False)
    monkeypatch.setenv("THROUGHLINE_OWNER_TZ", "America/Winnipeg")
    assert str(agent_view.owner_zone()) == "America/Winnipeg"
    from throughline import cadence, people
    from throughline.onboarding import starter

    # The deprecated alias is read everywhere the setting is.
    assert people.timezone() == "America/Winnipeg"
    assert str(cadence.zone()) == "America/Winnipeg"
    assert starter._tz() == "America/Winnipeg"
    monkeypatch.setenv("THROUGHLINE_TIMEZONE", "Europe/Berlin")
    assert str(agent_view.owner_zone()) == "Europe/Berlin"
    assert people.timezone() == "Europe/Berlin"
    assert str(cadence.zone()) == "Europe/Berlin"
    assert starter._tz() == "Europe/Berlin"


def test_redact_hides_a_pgpassword_outside_the_url(monkeypatch):
    monkeypatch.setenv("THROUGHLINE_DATABASE_URL", "postgresql://postgres@db/throughline")
    monkeypatch.setenv("PGPASSWORD", "s3cr@t/pass:word")
    assert "s3cr@t" not in ops.redact("auth failed for s3cr@t/pass:word")
