# otel_dp — OTel logs + traces, Data Prepper index shape

Indexing-throughput / resilience workload for Mustang M2. Documents and mappings match what
Data Prepper's `otel_logs` / `otel_traces` sinks produce with the **standard** index templates:

| Alias / index | Template | Notes |
|---|---|---|
| `logs-otel-v1` → `logs-otel-v1-000001…` | `logs-otel-v1-index-standard-template.json` | unchanged |
| `otel-v1-apm-span` → `otel-v1-apm-span-000001…` | `otel-v1-apm-span-index-standard-template.json` | **+ keyword dynamic templates for `events.attributes.*` / `links.attributes.*`** |
| `otel-v2-apm-service-map` | `otel-v2-apm-service-map-index-template.json` | unchanged, 1 shard |

The span-template deviation is required on composite (Mustang) indices: stock DP leaves
`events.attributes.*` to default dynamic mapping (`text` + `.keyword`) and composite rejects
`text` under `nested` (`fields there cannot be searched. Use [keyword] instead`).

Writes go to ISM rollover aliases (policy `otel-dp-rollover`, `min_primary_shard_size` 50gb),
like Data Prepper does.

## Corpus

No `base-url` — the corpus is generated locally with `scripts/otel_dp_ingest.py corpus`:
synthetic OTel Demo traffic (each trace fans out over 3–8 spans across the demo services,
~1 log per span sharing its traceId/spanId, ~3% error traces with exception events).
Place files at `<data dir>/otel_dp_logs/logs.json`, `otel_dp_spans/spans.json`,
`otel_dp_service_map/service-map.json` and pass the counts from `corpus-meta.json`.

## Procedures

- `setup` — deletes **only** `logs-otel-v1-*`, `otel-v1-apm-span-*`, `otel-v2-apm-service-map`;
  installs templates + ISM policy; bootstraps write aliases; loads the service map.
- `ingest` (default) — parallel looped bulk of logs + spans for `time_period` seconds.

## Parameters

| Param | Default |
|---|---|
| `logs_document_count` / `spans_document_count` / `service_map_document_count` | 100000000 / 100000000 / 46 |
| `number_of_shards` / `number_of_replicas` / `refresh_interval` | 10 / 1 / unset (cluster default; AOS enforces a 5s minimum on explicit values) |
| `rollover_shard_size` | `50gb` |
| `bulk_size` | 5000 |
| `bulk_indexing_clients` (or `logs_clients` / `spans_clients`) | 48 each |
| `time_period` / `warmup_time_period` | 3600 / 0 |

Note: `setup` manages templates/indices with `raw-request` rather than OSB's
`create-/delete-composable-template` ops — in OSB 1.18 the delete op's param source only reads the
legacy `templates` list and its runner raises `KeyError: 'client_request_start'`.

## Running it (scripts/)

- `scripts/otel_dp_ingest.py corpus --out-dir DIR --spans N --seed S` — writes `logs.json`, `spans.json`,
  `service-map.json`, OSB `.offset` tables and `corpus-meta.json` (doc counts to pass as workload params).
  ~100M spans + ~100M logs ≈ 267 GB, ~25 min on a 32-vCPU box. Use a different `--seed` per load generator.
- `scripts/run_otel_dp_osb.sh` — drives this workload on a load-generator host: `setup` once (aborts on any
  error and verifies the Data Prepper mappings landed), then back-to-back `ingest` chunks of `CHUNK_SECONDS`
  with `node-stats` telemetry, until `TARGET_BYTES` primary or `DISK_STOP`% on any data node. Optional
  corpus rotation (`ROTATE=1`) generates the next corpus in the background — avoid it on the ingest box at
  full speed (costs ~15% throughput); prefer one corpus per load generator.

```bash
# on each load generator (OSB 1.18, benchmark.ini with an `ovi` workload repo + your results datastore)
python3 scripts/otel_dp_ingest.py corpus --out-dir /opt/otel/corpus --spans 100000000 --seed $(date +%s)
EP=https://<domain-endpoint> AUTH='<user>:<password>' RUNNER_TAG=runner-a \
  setsid nohup bash scripts/run_otel_dp_osb.sh > /opt/otel/osb-loop.log 2>&1 &
# additional generators: same, plus SKIP_SETUP=1 and their own RUNNER_TAG
# stop at the next chunk boundary: touch /opt/otel/STOP
```

Reference run (Mustang M2, 10× 8-vCPU data nodes): 2× c7i.8xlarge generators, `bulk_size` 5000,
48 clients/signal/generator (192 total), unthrottled → ~300–400k docs/s, cluster CPU-bound;
256 clients just deepened the write queue (~500) and caused 120 s timeouts. Client options
`timeout:120,max_retries:0` keep a hung bulk from stalling the end of a time-period chunk.

`scripts/otel_dp_ingest.py setup|run` is the original non-OSB driver (same docs/templates, aggregate docs/s
only) — kept for quick smoke loads; use OSB for anything you want latency/throughput metrics from.
