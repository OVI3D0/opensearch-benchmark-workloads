# OpenObserve one-billion-log workload

This workload adapts the reproducible Kubernetes-style observability benchmark
published by OpenObserve for OpenSearch Benchmark.

Upstream source:

- Repository: https://github.com/openobserve/openobserve-clickhouse-benchmark
- Revision: `289608a6f96e4c35783d2aafb680f0ba4dd406b8`
- Article:
  https://openobserve.ai/blog/openobserve-vs-clickhouse-one-billion-logs-benchmark/

The upstream benchmark defines a one-billion-record synthetic log corpus and 19
measured query shapes: eight count queries, eight corresponding latest-100 row
queries, and three aggregations. This adaptation preserves the upstream runner's
execution order: count then row fetch for each of the first eight definitions,
followed by the three aggregations.

## Timestamp representation

The corpus preserves the upstream byte representation: `_timestamp` is epoch
microseconds stored as an unsigned integer. OpenSearch maps it as `long` and uses a
numeric histogram interval of `3,600,000,000` microseconds. ClickHouse stores it as
`UInt64`; DataFusion converts it with `to_timestamp_micros` only inside histogram
queries.

## Corpus

The full corpus is expected at:

`https://dbyiw3u3rf9yr.cloudfront.net/corpora/openobserve_1b/documents.json.gz`

OSB test mode expects:

`https://dbyiw3u3rf9yr.cloudfront.net/corpora/openobserve_1b/documents-1k.json.gz`

The corpus artifacts have not yet been uploaded. `corpus/capture.py` captures the
upstream generator's gzip request bodies without rewriting their full-corpus NDJSON
records. The committed 1,000-document manifest records all query anchors, q01-q19
goldens, timestamp bounds, and the local artifact's SHA-256.

## Procedures

- `openobserve-1b-mustang`: composite-index ingest plus PPL queries.
- `openobserve-1b-mustang-ingest`: composite-index ingest only.
- `openobserve-1b-mustang-queries`: PPL queries only.
- `openobserve-1b-vanilla`: Lucene ingest plus Query DSL queries.
- `openobserve-1b-vanilla-ingest`: Lucene ingest only.
- `openobserve-1b-vanilla-queries`: Query DSL queries only.
- `openobserve-1b-clickhouse-queries`: ClickHouse queries only. The split ClickHouse
  ingest procedure will be added when the multi-engine ClickHouse workload support is
  rebased onto this branch.

Native DataFusion execution reads `datafusion/queries.sql` from the same immutable
workload revision and keeps `SessionContext` startup outside measured time.

## Reporting

Report OSB percentile **service time**, never OSB latency. Present q01 through q19
in execution order.
