import json
import shutil
from pathlib import Path

from throughline_capture import Capture, screen_message, transcript_message

RECORDED = Path(__file__).parent / "recorded" / "spool"


def test_messages_are_timeline_shaped():
    item = json.loads(next((RECORDED / "screens").glob("*.json")).read_text())
    message = screen_message(item, "asset-1")
    assert message["channel"] == "capture.screens"
    assert message["content"].startswith("Screen: code — ")
    assert message["fields"]["immich_asset_id"] == "asset-1"
    assert message["thread"]["external_id"].startswith("synthbox:")
    item = json.loads(next((RECORDED / "transcripts").glob("*.json")).read_text())
    message = transcript_message(item)
    assert message["ended_at"] and message["content"].startswith("Synthetic orchard")


def test_manifest_embeds_transcripts_not_screens():
    channels = {c.name: c for c in Capture.manifest.channels}
    assert channels["capture.screens"].embed is False
    assert channels["capture.transcripts"].embed is None


def test_spool_is_emptied_after_the_checkpoint(tmp_path, monkeypatch):
    spool = tmp_path / "spool"
    shutil.copytree(RECORDED, spool)
    monkeypatch.setenv("CAPTURE_SPOOL_DIR", str(spool))
    monkeypatch.setenv("CAPTURE_IMMICH_FIXTURE", "1")
    monkeypatch.setenv("CAPTURE_BATCH", "3")
    plugin = Capture()
    plugin.dry_run = False
    seen = []
    for item in plugin.records({}, "sync"):
        seen.append(item)
        if type(item).__name__ == "Checkpoint":
            # Nothing of the batch is gone before the runner stored the checkpoint.
            assert any(spool.rglob("*.json"))
    assert not list(spool.rglob("*.json")) and not list(spool.rglob("*.jpg"))
    messages = [i for i in seen if isinstance(i, dict)]
    assert len(messages) == 8
    assert all(m["fields"]["immich_asset_id"] for m in messages if m["channel"] == "capture.screens")


def test_contract_kit(tmp_path):
    from throughline_sdk.contract import check_plugin

    spool = tmp_path / "spool"
    shutil.copytree(RECORDED, spool)
    check_plugin(
        "throughline_capture:Capture",
        {
            "CAPTURE_SPOOL_DIR": str(spool),
            "CAPTURE_IMMICH_FIXTURE": "1",
            "CAPTURE_DELETE_AFTER_UPLOAD": "false",
            "CAPTURE_BATCH": "2",
        },
    )
