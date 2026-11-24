import random
import time
from concurrent.futures import ThreadPoolExecutor

from conftest import ingest, message, truncate_tl

from throughline.db import connect


def test_msg_4_concurrent_batches(client, channel):
    rows = [message(i) for i in range(90)]
    random.Random(24).shuffle(rows)
    with ThreadPoolExecutor(max_workers=3) as pool:
        responses = list(
            pool.map(
                lambda chunk: ingest(client, chunk), [rows[:40], rows[30:70], rows[60:]]
            )
        )
    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    with connect() as c:
        rows = c.execute("SELECT * FROM api.messages ORDER BY thread_no").fetchall()
        assert len(rows) == 90
        assert [r["external_id"] for r in rows] == [f"fake-{i}" for i in range(90)]


def test_idn_2_msg_1_throughput_20000(client, channel, record_property):
    rows = [
        message(i, thread={"external_id": f"fake-thread-{i % 8}"}) for i in range(20000)
    ]
    start = time.perf_counter()
    for i in range(0, len(rows), 5000):
        r = ingest(client, rows[i : i + 5000])
        assert r.status_code == 200, r.text
        assert r.json()["inserted"] == 5000
    elapsed = time.perf_counter() - start
    rate = 20000 / elapsed
    record_property("messages_per_second", round(rate, 2))
    print(f"\nIdentity-enabled HTTP ingest: {rate:.2f} messages/second")
    assert rate >= 500
    with connect() as c:
        assert (
            c.execute(
                "SELECT count(*) n FROM api.messages WHERE author_identifier_id IS NOT NULL"
            ).fetchone()["n"]
            == 20000
        )


def test_idn_2_resolution_overhead(client, channel, record_property):
    import gc
    import statistics

    from throughline.ingest import ingest as core_ingest
    from throughline.models import Ingest

    # Compare identical payloads with lookup enabled/disabled internally. Alternate
    # ordering and take medians to reduce filesystem/checkpoint timing noise.
    on = Ingest(plugin="synthetic", messages=[message(i) for i in range(5000)])
    timings = {"on": [], "off": []}
    # The test process holds every collected test and module; in a full run that
    # heap is large and Python's garbage collector rescans it during ingest, which
    # measures the harness rather than identity lookup. Freeze it while timing.
    gc.collect()
    gc.freeze()
    try:
        for order in (("off", "on"), ("on", "off"), ("off", "on")):
            for mode in order:
                with connect() as c:
                    # Everything but the registered channel and the token.
                    truncate_tl(c, keep=("channels", "tokens"))
                    # Flush the previous sample's writes outside the timed region,
                    # so a checkpoint they start cannot land inside the next one.
                    c.execute("CHECKPOINT")
                start = time.perf_counter()
                with connect("app") as c:
                    assert (
                        core_ingest(c, on, resolve_identity=mode == "on")["inserted"]
                        == 5000
                    )
                timings[mode].append(time.perf_counter() - start)
    finally:
        gc.unfreeze()
    ratio = statistics.median(timings["on"]) / statistics.median(timings["off"])
    record_property("identity_overhead_ratio", round(ratio, 3))
    print(f"\nIdentity overhead ratio: {ratio:.3f}")
    assert ratio <= 1.1
