"""One shared identity-quality order for ingest and the dedup stage."""

from throughline import identity_quality, ingest
from throughline.retrieval.stages import dedup


def test_default_order_and_shared_definition(monkeypatch):
    monkeypatch.delenv("THROUGHLINE_IDENTITY_QUALITY_ORDER", raising=False)
    assert identity_quality.order() == ("native_id", "bridge_id", "fingerprint", "unknown")
    assert dedup.quality_order() == identity_quality.order()
    ranks = [ingest.quality_rank(q) for q in ("native_id", "bridge_id", "fingerprint", "unknown", None)]
    assert ranks == sorted(ranks, reverse=True) and ranks[-1] == 0
    positions = [dedup.quality_rank(q) for q in ("native_id", "bridge_id", "fingerprint", "unknown", "other")]
    assert positions == [0, 1, 2, 3, 4]


def test_stored_legacy_name_keeps_its_precedence(monkeypatch):
    monkeypatch.delenv("THROUGHLINE_IDENTITY_QUALITY_ORDER", raising=False)
    for legacy, generic in identity_quality.LEGACY_ALIASES.items():
        assert ingest.quality_rank(legacy) == ingest.quality_rank(generic)
        assert dedup.quality_rank(legacy) == dedup.quality_rank(generic)


def test_env_override(monkeypatch):
    monkeypatch.setenv("THROUGHLINE_IDENTITY_QUALITY_ORDER", "fingerprint, native_id")
    assert identity_quality.order() == ("fingerprint", "native_id")
    assert ingest.quality_rank("fingerprint") > ingest.quality_rank("native_id") > 0
    assert ingest.quality_rank("bridge_id") == 0
    assert dedup.quality_rank("fingerprint") == 0


def test_catalogue_text_is_neutral_on_fresh_and_upgraded_databases(database):
    from pathlib import Path

    from throughline.db import connect

    migration = (
        Path(identity_quality.__file__).parent / "migrations" / "0079_neutral_catalogue_text.sql"
    ).read_text()

    def texts(c):
        arc = c.execute("SELECT description FROM tl.api_functions WHERE name='person_arc'").fetchone()
        aliases = c.execute("SELECT body::text AS b FROM tl.catalog_sections WHERE subject='message_aliases'").fetchone()
        return arc["description"], aliases["b"]

    with connect() as c:
        fresh = texts(c)
        assert fresh[0].startswith("Contact arc: per period")
        assert "native_id, bridge_id, fingerprint, unknown" in fresh[1]
        # An upgraded database still carries the older wording: 0076 fixes it.
        c.execute(
            "UPDATE tl.api_functions SET description=replace(description,'Contact arc:','Sam''s arc:') WHERE name='person_arc'"
        )
        c.execute(
            "UPDATE tl.catalog_sections SET body=replace(body::text,'bridge_id, fingerprint','relay_id, fingerprint')::jsonb WHERE subject='message_aliases'"
        )
        c.execute(migration)
        assert texts(c) == fresh
