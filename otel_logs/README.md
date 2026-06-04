# OTel Logs PPL Benchmark Workload

A search-only OpenSearch Benchmark workload that runs 39 PPL queries against an existing OTel-shaped logs index.

## What this workload does

- **Does NOT ingest data.** You load the OTel logs index yourself (e.g., via Data Prepper from telemetrygen, restored from a snapshot, or any other path).
- **Does run 39 PPL queries** covering counts, group-by, where-clauses, eval expressions, dedup, top/rare, head, sort, span aggregations, and multi-field analytics.
- **Reports per-query throughput and latency** so you can compare engines or cluster configurations on the same dataset.

## Prerequisites

- An OpenSearch cluster with the SQL/PPL plugin (default in OpenSearch).
- An index (default name `otel_logs`, overridable) populated with documents matching the OTel logs mapping.

## Running

Default index name (`otel_logs`):
```bash
opensearch-benchmark run \
  --workload=otel_logs \
  --pipeline=benchmark-only \
  --target-hosts=<endpoint>:443 \
  --client-options=use_ssl:true,verify_certs:false,basic_auth_user:<user>,basic_auth_password:<pw>
```

Custom index name (e.g., `otel-logs-ec2-*` from a multi-source ingest):
```bash
opensearch-benchmark run \
  --workload=otel_logs \
  --workload-params='{"index_name":"otel-logs-ec2-*"}' \
  --pipeline=benchmark-only \
  --target-hosts=<endpoint>:443 \
  --client-options=...
```

Quick smoke test (1 iteration per query, no warmup):
```bash
opensearch-benchmark run \
  --workload=otel_logs \
  --test-procedure=otel-logs-ppl-test \
  --workload-params='{"warmup_iterations":0,"test_iterations":1}' \
  --pipeline=benchmark-only \
  --target-hosts=...
```

## Workload parameters

| Param | Default | What it controls |
|---|---|---|
| `index_name` | `otel_logs` | Index to query against. Use a wildcard like `otel-logs-ec2-*` for multi-index corpora. |
| `legacy_syntax` | `false` | PPL legacy vs modern syntax (sets cluster setting `plugins.ppl.syntax.legacy.preferred`). |
| `warmup_iterations` | `20` | Warmup iterations per query. |
| `test_iterations` | `20` | Measured iterations per query. |
| `target_throughput` | `2` | ops/sec target per query. |
| `search_clients` | `1` | Concurrent clients per query. |
| Per-query overrides | `qNN_warmup_iterations`, `qNN_iterations`, `qNN_target_throughput`, `qNN_clients` for `NN` in `01..39` |

## Test procedures

| Name | Default | What it does |
|---|---|---|
| `otel-logs-ppl` | yes | Full run: 20 warmup + 20 measured iterations of each of 39 queries. Per-query stats. |
| `otel-logs-ppl-test` | no | Lightweight: same schedule, intended for use with `test_iterations:1`. |
| `otel-logs-random-mix` | no | Sustained random query mix at a TPS target. Each request picks one of the 39 PPL queries uniformly at random. Aggregate stats only (no per-query breakdown). For chaos testing or steady-state load. |

### Random-mix parameters

| Param | Default | What it controls |
|---|---|---|
| `warmup_time_period` | `60` | Warmup seconds before measurement starts. |
| `time_period` | `1800` | Measured run duration in seconds. |
| `target_throughput` | `10` | Aggregate ops/sec target across all clients. |
| `search_clients` | `4` | Concurrent clients firing the random mix. |

Random-mix usage:
```bash
opensearch-benchmark run --workload=otel_logs \
  --test-procedure=otel-logs-random-mix \
  --workload-params='{"index_name":"otel-logs-ec2-*","time_period":3600,"target_throughput":20,"search_clients":8}' \
  --pipeline=benchmark-only --target-hosts=...
```

## Source of queries

The 39 queries are auto-generated from upstream PPL test resources and live in `operations/default.json`. Each operation is named `qNN-<short-desc>` so benchmark output is self-describing.

Two queries are documented gaps in some PPL implementations:

| Query | Notes |
|---|---|
| q32 (`where severityText = 'ERROR'`) | Some engines route text-equality predicates to a path that bypasses the keyword sub-field, returning 0 rows even when matching documents exist. Use the `like` form (q05) as a workaround. |
| q33 (`... \| head 0`) | Upstream SQL plugin issue — `ClassCastException` in `UnifiedQueryPlanner.preserveCollation` on some builds. |

## Comparison runs

Run the same workload against multiple endpoints and diff the per-query latency stats:

```bash
# Run 1: cluster A
opensearch-benchmark run --workload=otel_logs \
  --workload-params='{"index_name":"otel-logs-ec2-*"}' \
  --target-hosts=<endpoint-A>:443 \
  --user-tag="cluster:A" \
  ... > a.log

# Run 2: cluster B (same data shape)
opensearch-benchmark run --workload=otel_logs \
  --workload-params='{"index_name":"otel-logs-ec2-*"}' \
  --target-hosts=<endpoint-B>:443 \
  --user-tag="cluster:B" \
  ... > b.log

# Compare percentile latencies for each query in the OpenSearch Benchmark summary tables
```
