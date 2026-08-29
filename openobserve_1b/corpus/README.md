# Corpus capture

`capture.py` checks out the pinned upstream benchmark generator, runs its ClickHouse
target against a local HTTP sink, and concatenates the accepted gzip request bodies
into an OSB NDJSON corpus. Full-corpus records are not parsed or rewritten.

Full capture:

```bash
python3 corpus/capture.py \
  --output documents.json.gz \
  --records 1000000000 \
  --batch-size 8000 \
  --concurrency 6
```

Test-mode capture uses the one-billion-row cardinality universe but stops after its
first 1,000-row batch. Timestamps are spread over the published 4h13m duration so
histogram plumbing is exercised across five hour buckets:

```bash
python3 corpus/capture.py \
  --output documents-1k.json.gz \
  --records 1000000000 \
  --batch-size 1000 \
  --concurrency 1 \
  --capture-batches 1 \
  --spread-duration-seconds 15180
```

Each command writes a sibling `.manifest.json` with row/byte counts, SHA-256,
provenance, and query anchors. The committed smoke manifest additionally records
expected q01–q19 results.

## Full corpus: bounded direct-to-S3 capture

The full one-billion-row publication uses one continuous upstream generator process
and rolls its gzip request bodies into independently checksummed chunks. Closed
chunks upload concurrently and are removed from local disk after S3 size
verification. `manifest.json` is uploaded last and is the publication-complete
marker; a failed generator run never publishes that manifest.

```bash
python3 corpus/capture_to_s3.py \
  --s3-uri s3://opensearch-benchmark-workloads/corpora/openobserve_1b/v1 \
  --work-dir /data/openobserve-1b \
  --records 1000000000 \
  --batch-size 8000 \
  --concurrency 12 \
  --chunk-rows 10000000
```

The destination prefix must be empty. The script refuses to replace existing
objects, verifies every uploaded object's byte length, and records each chunk's row
count, compressed/uncompressed sizes, SHA-256, and timestamp bounds.
