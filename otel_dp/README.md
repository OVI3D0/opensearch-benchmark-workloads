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

No `base-url` — the corpus is generated locally with
`MustangBenchmarkConfigResults/scripts/otel-loadtest/otel_dp_ingest.py corpus`:
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
