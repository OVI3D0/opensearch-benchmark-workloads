import gzip
import json
from pathlib import Path

import pytest

from capture_to_s3 import ChunkCapture, _batch_stats, _parse_s3_uri


def make_batch(start: int, rows: int) -> bytes:
    raw = b"".join(
        json.dumps({"_timestamp": start + offset, "value": offset}).encode() + b"\n"
        for offset in range(rows)
    )
    return gzip.compress(raw, mtime=0)


def test_batch_stats_reads_rows_bytes_and_timestamp_bounds():
    body = make_batch(100, 3)

    rows, raw_bytes, timestamp_min_us, timestamp_max_us = _batch_stats(body)

    assert rows == 3
    assert raw_bytes == len(gzip.decompress(body))
    assert timestamp_min_us == 100
    assert timestamp_max_us == 102


def test_chunk_capture_rolls_and_uploads_concatenated_gzip(tmp_path: Path):
    uploaded: dict[str, bytes] = {}

    def upload(path: Path, uri: str, expected_bytes: int) -> None:
        uploaded[uri] = path.read_bytes()
        assert len(uploaded[uri]) == expected_bytes

    capture = ChunkCapture(
        tmp_path,
        "s3://bucket/corpus/v1",
        chunk_rows=4,
        upload_workers=1,
        upload_func=upload,
    )
    for start in (100, 102, 104, 106):
        capture.append(make_batch(start, 2))

    chunks = capture.finish()

    assert [chunk.rows for chunk in chunks] == [4, 4]
    assert [chunk.name for chunk in chunks] == [
        "part-00000.json.gz",
        "part-00001.json.gz",
    ]
    assert len(uploaded) == 2
    first = uploaded["s3://bucket/corpus/v1/ndjson/part-00000.json.gz"]
    assert len(gzip.decompress(first).splitlines()) == 4


def test_chunk_capture_rejects_batch_crossing_boundary(tmp_path: Path):
    capture = ChunkCapture(
        tmp_path,
        "s3://bucket/corpus/v1",
        chunk_rows=3,
        upload_workers=1,
        upload_func=lambda *_args: None,
    )
    capture.append(make_batch(100, 2))

    with pytest.raises(RuntimeError, match="crosses"):
        capture.append(make_batch(102, 2))

    capture.stop_after_failure()


def test_parse_s3_uri():
    assert _parse_s3_uri("s3://bucket/a/b") == ("bucket", "a/b")
