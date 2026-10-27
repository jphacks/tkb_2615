"""The frame cache keeps within the storage capacity.

That cached frames equal reconstructed ones is checked in
tests/integration/test_timeline_frame_cache.py.
"""

from sheltersim.storage.frames import FrameCache


def test_cache_respects_storage_capacity(tmp_path, saved_timeline):
    store, timeline = saved_timeline(tmp_path)
    reference = store.branch_reference("p", "r", "main")
    cache = FrameCache(store, "p", "r")
    cache._file(reference.checkpoint_hash).unlink()
    store.max_bytes = store.usage()["bytes"] + 1
    assert cache.put(reference.checkpoint_hash, {"minute": 0}) is False
    assert cache.get(reference.checkpoint_hash) is None
