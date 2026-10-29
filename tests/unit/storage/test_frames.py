"""The frame cache keeps within the storage capacity and writes canonical JSON.

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


def test_a_cached_frame_file_is_the_canonical_json_of_its_document(tmp_path, saved_timeline):
    """The frame's JSON is encoded once and spliced into the document's."""
    import gzip
    import hashlib

    from sheltersim.storage.frames import FRAME_PROJECTION_VERSION
    from sheltersim.storage.projects import canonical

    store, _ = saved_timeline(tmp_path)
    reference = store.branch_reference("p", "r", "main")
    cache = FrameCache(store, "p", "r")
    cache._file(reference.checkpoint_hash).unlink()
    frame = {"minute": 0, "agents": [{"id": "A001", "zone": "避難所", "x": 1.5}], "z": None}
    assert cache.put(reference.checkpoint_hash, frame) is True
    document = {
        "schema_version": 1,
        "projection_version": FRAME_PROJECTION_VERSION,
        "checkpoint_hash": reference.checkpoint_hash,
        "frame_hash": hashlib.sha256(canonical(frame).encode()).hexdigest(),
        "frame": frame,
    }
    raw = gzip.decompress(cache._file(reference.checkpoint_hash).read_bytes())
    assert raw == (canonical(document) + "\n").encode()
    assert cache.get(reference.checkpoint_hash) == frame
