from factor.memory_util import memory_snapshot, rss_mb


def test_memory_snapshot_shape():
    snap = memory_snapshot()
    assert "rss_mb" in snap
    assert "gc_counts" in snap
    assert "pid" in snap
    # rss may be None on some platforms; if present, sane range
    mb = rss_mb()
    if mb is not None:
        assert 0 < mb < 10_000
