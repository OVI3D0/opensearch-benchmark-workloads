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
