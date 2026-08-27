#!/usr/bin/env python3
"""Capture the pinned upstream generator's ClickHouse gzip batches as an OSB corpus."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


UPSTREAM_URL = "https://github.com/openobserve/openobserve-clickhouse-benchmark.git"
UPSTREAM_SHA = "289608a6f96e4c35783d2aafb680f0ba4dd406b8"
DEFAULT_START_TIMESTAMP_US = 1780272000000000


class CaptureState:
    def __init__(self, output: Path, max_batches: int | None) -> None:
        self.output = output
        self.max_batches = max_batches
        self.batches = 0
        self.compressed_bytes = 0
        self.lock = threading.Lock()
        self.complete = threading.Event()

    def append(self, body: bytes) -> bool:
        with self.lock:
            if self.max_batches is not None and self.batches >= self.max_batches:
                return False
            with self.output.open("ab") as out:
                out.write(body)
            self.batches += 1
            self.compressed_bytes += len(body)
            if self.max_batches is not None and self.batches >= self.max_batches:
                self.complete.set()
            return True


def handler_for(state: CaptureState):
    class CaptureHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            if self.headers.get("Content-Encoding") != "gzip":
                self.send_error(400, "generator must use gzip")
                return
            if not state.append(body):
                self.send_error(503, "capture complete")
                return
            self.send_response(200)
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return CaptureHandler


def checkout_upstream(checkout: Path) -> None:
    if not checkout.exists():
        subprocess.run(
            ["git", "clone", "--filter=blob:none", UPSTREAM_URL, str(checkout)],
            check=True,
        )
    subprocess.run(["git", "-C", str(checkout), "fetch", "origin", UPSTREAM_SHA], check=True)
    subprocess.run(["git", "-C", str(checkout), "checkout", "--detach", UPSTREAM_SHA], check=True)
    actual = subprocess.check_output(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True
    ).strip()
    if actual != UPSTREAM_SHA:
        raise RuntimeError(f"unexpected upstream SHA: {actual}")


def build_generator(checkout: Path) -> Path:
    subprocess.run(
        ["cargo", "build", "--release", "--locked"],
        cwd=checkout / "datagen",
        check=True,
    )
    return checkout / "datagen" / "target" / "release" / "benchmark-data"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as src:
        while chunk := src.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def spread_timestamps(path: Path, start_timestamp_us: int, duration_seconds: int) -> None:
    rows: list[dict[str, object]] = []
    with gzip.open(path, "rt") as src:
        rows = [json.loads(line) for line in src]
    if len(rows) < 2:
        raise RuntimeError("timestamp spreading requires at least two rows")
    duration_us = duration_seconds * 1_000_000
    rewritten = path.with_suffix(path.suffix + ".rewrite")
    with rewritten.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as out:
            for ordinal, row in enumerate(rows):
                row["_timestamp"] = start_timestamp_us + (
                    ordinal * duration_us // (len(rows) - 1)
                )
                out.write(
                    json.dumps(row, separators=(",", ":"), ensure_ascii=False).encode()
                    + b"\n"
                )
    os.replace(rewritten, path)


def inspect_corpus(path: Path) -> tuple[int, int, int, int, dict[str, str]]:
    rows = 0
    uncompressed_bytes = 0
    first: dict[str, str] | None = None
    common_token_seen = False
    timestamp_min_us: int | None = None
    timestamp_max_us: int | None = None
    with gzip.open(path, "rb") as src:
        for line in src:
            rows += 1
            uncompressed_bytes += len(line)
            row = json.loads(line)
            if first is None:
                first = row
            timestamp = int(row["_timestamp"])
            timestamp_min_us = (
                timestamp if timestamp_min_us is None else min(timestamp_min_us, timestamp)
            )
            timestamp_max_us = (
                timestamp if timestamp_max_us is None else max(timestamp_max_us, timestamp)
            )
            if b"failed" in line.lower():
                common_token_seen = True
    if first is None or timestamp_min_us is None or timestamp_max_us is None:
        raise RuntimeError("captured corpus is empty")
    if not common_token_seen:
        raise RuntimeError("captured corpus does not contain the common token 'failed'")
    anchors = {
        "trace_id": first["trace_id"],
        "span_id": first["span_id"],
        "rare_token": first["request_id"],
        "common_token": "failed",
        "container": first["kubernetes_container_name"],
        "pod_name": first["kubernetes_pod_name"],
    }
    return rows, uncompressed_bytes, timestamp_min_us, timestamp_max_us, anchors


def write_metadata(
    output: Path,
    expected_rows: int,
    stats: dict[str, object] | None,
    capture: dict[str, int | None],
) -> None:
    if expected_rows <= 1_000_000:
        (
            rows,
            uncompressed_bytes,
            timestamp_min_us,
            timestamp_max_us,
            anchors,
        ) = inspect_corpus(output)
        if rows != expected_rows:
            raise RuntimeError(f"expected {expected_rows} rows, captured {rows}")
    else:
        if stats is None:
            raise RuntimeError("full corpus metadata requires completed generator stats")
        rows = expected_rows
        uncompressed_bytes = int(stats["raw_bytes"])
        timestamp_min_us = int(stats["timestamp_min_us"])
        timestamp_max_us = int(stats["timestamp_max_us"])
        anchors = json.loads(
            (Path(__file__).resolve().parents[1] / "anchors.json").read_text()
        )
    metadata = {
        "upstream_url": UPSTREAM_URL,
        "upstream_sha": UPSTREAM_SHA,
        "source_file": output.name,
        "document_count": rows,
        "compressed_bytes": output.stat().st_size,
        "uncompressed_bytes": uncompressed_bytes,
        "sha256": file_sha256(output),
        "timestamp_min_us": timestamp_min_us,
        "timestamp_max_us": timestamp_max_us,
        "anchors": anchors,
        "capture": capture,
    }
    output.with_suffix(output.suffix + ".manifest.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--records", type=int, default=1_000_000_000)
    parser.add_argument("--batch-size", type=int, default=8_000)
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--seed", type=int, default=0xC0FFEE)
    parser.add_argument("--start-timestamp-us", type=int, default=DEFAULT_START_TIMESTAMP_US)
    parser.add_argument(
        "--spread-duration-seconds",
        type=int,
        help="rewrite captured timestamps across this duration (intended for test mode)",
    )
    parser.add_argument("--checkout", type=Path)
    parser.add_argument(
        "--capture-batches",
        type=int,
        help="capture only the first N batches from the requested total, then stop",
    )
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.records <= 0 or args.batch_size <= 0 or args.concurrency <= 0:
        raise SystemExit("records, batch-size, and concurrency must be positive")
    if args.capture_batches is not None and args.capture_batches <= 0:
        raise SystemExit("capture-batches must be positive")
    if args.capture_batches is not None and args.concurrency != 1:
        raise SystemExit("capture-batches requires --concurrency 1")
    if args.spread_duration_seconds is not None and args.spread_duration_seconds <= 0:
        raise SystemExit("spread-duration-seconds must be positive")
    if args.spread_duration_seconds is not None and args.capture_batches is None:
        raise SystemExit("spread-duration-seconds requires --capture-batches")
    if args.output.exists():
        if not args.force:
            raise SystemExit(f"{args.output} exists; pass --force to replace it")
        args.output.unlink()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    partial = args.output.with_suffix(args.output.suffix + ".part")
    if partial.exists():
        partial.unlink()

    with tempfile.TemporaryDirectory(prefix="openobserve-1b-") as temp_dir:
        checkout = args.checkout or Path(temp_dir) / "upstream"
        checkout_upstream(checkout)
        generator = build_generator(checkout)
        state = CaptureState(partial, max_batches=args.capture_batches)
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
        stats: dict[str, object] | None = None
        try:
            if args.capture_batches is None:
                subprocess.run(command, check=True)
                stats = json.loads(stats_path.read_text())
            else:
                process = subprocess.Popen(command)
                if not state.complete.wait(timeout=300):
                    process.terminate()
                    process.wait(timeout=30)
                    raise RuntimeError("timed out waiting for requested capture batches")
                process.terminate()
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=30)
                time.sleep(0.1)
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join()

        if stats is not None:
            if (
                stats.get("records_sent") != args.records
                or stats.get("records_failed") != 0
                or stats.get("clickhouse_failed") != 0
            ):
                raise RuntimeError(f"generator did not complete cleanly: {stats}")
            expected_rows = args.records
        else:
            expected_rows = min(args.records, args.batch_size * args.capture_batches)
        os.replace(partial, args.output)
        if args.spread_duration_seconds is not None:
            spread_timestamps(
                args.output,
                args.start_timestamp_us,
                args.spread_duration_seconds,
            )
        write_metadata(
            args.output,
            expected_rows,
            stats,
            {
                "generator_total": args.records,
                "batch_size": args.batch_size,
                "concurrency": args.concurrency,
                "seed": args.seed,
                "start_timestamp_us": args.start_timestamp_us,
                "capture_batches": args.capture_batches,
                "spread_duration_seconds": args.spread_duration_seconds,
            },
        )
        print(
            json.dumps(
                {
                    "output": str(args.output),
                    "records": expected_rows,
                    "batches": state.batches,
                    "compressed_bytes": args.output.stat().st_size,
                },
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
