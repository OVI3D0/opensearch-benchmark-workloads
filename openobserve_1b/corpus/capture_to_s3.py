#!/usr/bin/env python3
"""Capture one continuous upstream generator run into bounded S3 corpus chunks."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import queue
import re
import subprocess
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from capture import (
    DEFAULT_START_TIMESTAMP_US,
    UPSTREAM_SHA,
    UPSTREAM_URL,
    build_generator,
    checkout_upstream,
)


@dataclass(frozen=True)
class Chunk:
    name: str
    rows: int
    compressed_bytes: int
    uncompressed_bytes: int
    sha256: str
    timestamp_min_us: int
    timestamp_max_us: int
    s3_uri: str


def _batch_stats(body: bytes) -> tuple[int, int, int, int]:
    raw = gzip.decompress(body)
    if not raw or not raw.endswith(b"\n"):
        raise RuntimeError("generator body is not newline-terminated NDJSON")
    rows = raw.count(b"\n")
    lines = raw.splitlines()
    first = json.loads(lines[0])
    last = json.loads(lines[-1])
    return rows, len(raw), int(first["_timestamp"]), int(last["_timestamp"])


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc:
        raise ValueError(f"expected s3://bucket/prefix, got {uri!r}")
    return parsed.netloc, parsed.path.strip("/")


class ChunkCapture:
    """Roll concatenated-gzip request bodies into uploaded corpus chunks."""

    def __init__(
        self,
        work_dir: Path,
        s3_uri: str,
        chunk_rows: int,
        max_pending: int = 3,
        upload_workers: int = 2,
        upload_func: Callable[[Path, str, int], None] | None = None,
    ) -> None:
        self.work_dir = work_dir
        self.s3_uri = s3_uri.rstrip("/")
        self.chunk_rows = chunk_rows
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.pending: queue.Queue[tuple[Path, Chunk] | None] = queue.Queue(max_pending)
        self.upload_func = upload_func or self._upload_to_s3
        self.uploaded: list[Chunk] = []
        self.uploaded_lock = threading.Lock()
        self.upload_error: BaseException | None = None
        self.current_index = 0
        self.current_path: Path | None = None
        self.current_file = None
        self.current_rows = 0
        self.current_compressed_bytes = 0
        self.current_uncompressed_bytes = 0
        self.current_timestamp_min_us: int | None = None
        self.current_timestamp_max_us: int | None = None
        self.current_hash = hashlib.sha256()
        self.workers = [
            threading.Thread(target=self._upload_loop, daemon=True)
            for _ in range(upload_workers)
        ]
        for worker in self.workers:
            worker.start()

    def _raise_upload_error(self) -> None:
        if self.upload_error is not None:
            raise RuntimeError("S3 chunk upload failed") from self.upload_error

    def _open_current(self) -> None:
        name = f"part-{self.current_index:05d}.json.gz"
        self.current_path = self.work_dir / name
        if self.current_path.exists():
            raise RuntimeError(f"refusing to replace existing chunk {self.current_path}")
        self.current_file = self.current_path.open("xb")
        self.current_hash = hashlib.sha256()

    def append(self, body: bytes) -> None:
        rows, raw_bytes, timestamp_min_us, timestamp_max_us = _batch_stats(body)
        with self.lock:
            self._raise_upload_error()
            if self.current_file is None:
                self._open_current()
            if self.current_rows + rows > self.chunk_rows:
                raise RuntimeError(
                    f"batch of {rows} rows crosses {self.chunk_rows}-row chunk boundary; "
                    "choose a chunk size divisible by the generator batch size"
                )
            self.current_file.write(body)
            self.current_hash.update(body)
            self.current_rows += rows
            self.current_compressed_bytes += len(body)
            self.current_uncompressed_bytes += raw_bytes
            self.current_timestamp_min_us = (
                timestamp_min_us
                if self.current_timestamp_min_us is None
                else min(self.current_timestamp_min_us, timestamp_min_us)
            )
            self.current_timestamp_max_us = (
                timestamp_max_us
                if self.current_timestamp_max_us is None
                else max(self.current_timestamp_max_us, timestamp_max_us)
            )
            if self.current_rows == self.chunk_rows:
                self._queue_current()

    def _queue_current(self) -> None:
        if self.current_file is None or self.current_path is None:
            return
        self.current_file.flush()
        self.current_file.close()
        chunk = Chunk(
            name=self.current_path.name,
            rows=self.current_rows,
            compressed_bytes=self.current_compressed_bytes,
            uncompressed_bytes=self.current_uncompressed_bytes,
            sha256=self.current_hash.hexdigest(),
            timestamp_min_us=int(self.current_timestamp_min_us),
            timestamp_max_us=int(self.current_timestamp_max_us),
            s3_uri=f"{self.s3_uri}/ndjson/{self.current_path.name}",
        )
        self.pending.put((self.current_path, chunk))
        self.current_index += 1
        self.current_path = None
        self.current_file = None
        self.current_rows = 0
        self.current_compressed_bytes = 0
        self.current_uncompressed_bytes = 0
        self.current_timestamp_min_us = None
        self.current_timestamp_max_us = None
        self.current_hash = hashlib.sha256()

    def _upload_loop(self) -> None:
        while True:
            item = self.pending.get()
            try:
                if item is None:
                    return
                path, chunk = item
                if self.upload_error is None:
                    try:
                        self.upload_func(path, chunk.s3_uri, chunk.compressed_bytes)
                        path.unlink()
                        with self.uploaded_lock:
                            self.uploaded.append(chunk)
                        print(
                            f"uploaded {chunk.name}: rows={chunk.rows} "
                            f"compressed={chunk.compressed_bytes} sha256={chunk.sha256}",
                            flush=True,
                        )
                    except BaseException as error:  # propagate from worker thread
                        self.upload_error = error
            finally:
                self.pending.task_done()

    @staticmethod
    def _upload_to_s3(path: Path, s3_uri: str, expected_bytes: int) -> None:
        bucket, key = _parse_s3_uri(s3_uri)
        head = subprocess.run(
            [
                "aws",
                "s3api",
                "head-object",
                "--bucket",
                bucket,
                "--key",
                key,
                "--region",
                "us-east-1",
            ],
            capture_output=True,
            text=True,
        )
        if head.returncode == 0:
            raise RuntimeError(f"refusing to overwrite existing object {s3_uri}")
        subprocess.run(
            [
                "aws",
                "s3",
                "cp",
                str(path),
                s3_uri,
                "--region",
                "us-east-1",
                "--only-show-errors",
            ],
            check=True,
        )
        verified = json.loads(
            subprocess.check_output(
                [
                    "aws",
                    "s3api",
                    "head-object",
                    "--bucket",
                    bucket,
                    "--key",
                    key,
                    "--region",
                    "us-east-1",
                ],
                text=True,
            )
        )
        if int(verified["ContentLength"]) != expected_bytes:
            raise RuntimeError(
                f"uploaded size mismatch for {s3_uri}: "
                f"{verified['ContentLength']} != {expected_bytes}"
            )

    def finish(self) -> list[Chunk]:
        with self.lock:
            if self.current_rows:
                self._queue_current()
        self.pending.join()
        for _ in self.workers:
            self.pending.put(None)
        for worker in self.workers:
            worker.join()
        self._raise_upload_error()
        return sorted(self.uploaded, key=lambda chunk: chunk.name)

    def stop_after_failure(self) -> None:
        with self.lock:
            if self.current_file is not None:
                self.current_file.flush()
                self.current_file.close()
                self.current_file = None
        self.pending.join()
        for _ in self.workers:
            self.pending.put(None)
        for worker in self.workers:
            worker.join()


def handler_for(state: ChunkCapture):
    from http.server import BaseHTTPRequestHandler

    class CaptureHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            if self.headers.get("Content-Encoding") != "gzip":
                self.send_error(400, "generator must use gzip")
                return
            try:
                state.append(body)
            except Exception as error:
                self.send_error(500, str(error))
                return
            self.send_response(200)
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return CaptureHandler


def _safe_upload_file(path: Path, s3_uri: str) -> None:
    ChunkCapture._upload_to_s3(path, s3_uri, path.stat().st_size)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--s3-uri", required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--records", type=int, default=1_000_000_000)
    parser.add_argument("--batch-size", type=int, default=8_000)
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--chunk-rows", type=int, default=10_000_000)
    parser.add_argument("--max-pending", type=int, default=3)
    parser.add_argument("--upload-workers", type=int, default=2)
    parser.add_argument("--seed", type=int, default=0xC0FFEE)
    parser.add_argument("--start-timestamp-us", type=int, default=DEFAULT_START_TIMESTAMP_US)
    parser.add_argument("--checkout", type=Path)
    return parser.parse_args()


def main() -> None:
    from http.server import ThreadingHTTPServer
    import tempfile

    args = parse_args()
    if min(
        args.records,
        args.batch_size,
        args.concurrency,
        args.chunk_rows,
        args.max_pending,
        args.upload_workers,
    ) <= 0:
        raise SystemExit("numeric arguments must be positive")
    if args.chunk_rows % args.batch_size:
        raise SystemExit("--chunk-rows must be divisible by --batch-size")
    if args.records % args.batch_size:
        raise SystemExit("--records must be divisible by --batch-size")

    prefix_bucket, prefix_key = _parse_s3_uri(args.s3_uri)
    existing = subprocess.check_output(
        [
            "aws",
            "s3api",
            "list-objects-v2",
            "--bucket",
            prefix_bucket,
            "--prefix",
            f"{prefix_key.rstrip('/')}/",
            "--max-items",
            "1",
            "--region",
            "us-east-1",
            "--query",
            "KeyCount",
            "--output",
            "text",
        ],
        text=True,
    ).strip()
    if existing not in {"0", "None"}:
        raise SystemExit(f"refusing non-empty destination prefix {args.s3_uri}")

    args.work_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="openobserve-1b-") as temp_dir:
        checkout = args.checkout or Path(temp_dir) / "upstream"
        checkout_upstream(checkout)
        generator = build_generator(checkout)
        state = ChunkCapture(
            args.work_dir,
            args.s3_uri,
            args.chunk_rows,
            max_pending=args.max_pending,
            upload_workers=args.upload_workers,
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(state))
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        host, port = server.server_address
        stats_path = Path(temp_dir) / "ingest.json"
        command = [
            str(generator),
            "--target",
            "clickhouse",
            "--total",
            str(args.records),
            "--batch-size",
            str(args.batch_size),
            "--concurrency",
            str(args.concurrency),
            "--compress",
            "true",
            "--seed",
            str(args.seed),
            "--start-timestamp-us",
            str(args.start_timestamp_us),
            "--ch-url",
            f"http://{host}:{port}",
            "--stats-out",
            str(stats_path),
        ]
        try:
            subprocess.run(command, check=True)
            stats = json.loads(stats_path.read_text())
            chunks = state.finish()
        except BaseException:
            state.stop_after_failure()
            raise
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join()

    if (
        int(stats["records_sent"]) != args.records
        or int(stats["records_failed"]) != 0
        or int(stats["clickhouse_failed"]) != 0
    ):
        raise RuntimeError(f"generator did not complete cleanly: {stats}")
    if sum(chunk.rows for chunk in chunks) != args.records:
        raise RuntimeError("uploaded chunk row total does not match requested records")
    if sum(chunk.uncompressed_bytes for chunk in chunks) != int(stats["raw_bytes"]):
        raise RuntimeError("uploaded chunk raw-byte total does not match generator stats")

    corpus_root = Path(__file__).resolve().parents[1]
    anchors_path = corpus_root / "anchors.json"
    schema_path = corpus_root / "index-datafusion.json"
    manifest = {
        "format_version": 1,
        "state": "complete",
        "upstream_url": UPSTREAM_URL,
        "upstream_sha": UPSTREAM_SHA,
        "generator": {
            "records": args.records,
            "batch_size": args.batch_size,
            "concurrency": args.concurrency,
            "seed": args.seed,
            "start_timestamp_us": args.start_timestamp_us,
            "stats": stats,
        },
        "chunk_rows": args.chunk_rows,
        "document_count": sum(chunk.rows for chunk in chunks),
        "compressed_bytes": sum(chunk.compressed_bytes for chunk in chunks),
        "uncompressed_bytes": sum(chunk.uncompressed_bytes for chunk in chunks),
        "timestamp_min_us": min(chunk.timestamp_min_us for chunk in chunks),
        "timestamp_max_us": max(chunk.timestamp_max_us for chunk in chunks),
        "anchors_sha256": hashlib.sha256(anchors_path.read_bytes()).hexdigest(),
        "schema_sha256": hashlib.sha256(schema_path.read_bytes()).hexdigest(),
        "chunks": [asdict(chunk) for chunk in chunks],
    }
    manifest_path = args.work_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    _safe_upload_file(manifest_path, f"{args.s3_uri.rstrip('/')}/manifest.json")
    print(
        json.dumps(
            {
                "manifest": f"{args.s3_uri.rstrip('/')}/manifest.json",
                "chunks": len(chunks),
                "records": manifest["document_count"],
                "compressed_bytes": manifest["compressed_bytes"],
            },
            sort_keys=True,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
