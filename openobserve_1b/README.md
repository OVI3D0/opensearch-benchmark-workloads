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

The immutable full corpus is published as 100 ten-million-row gzip NDJSON objects
under:

`https://dbyiw3u3rf9yr.cloudfront.net/corpora/openobserve_1b/v1/ndjson/`

Pass workload parameter `full_corpus:true` to select those objects. The default
single-file declaration is retained so OSB `--test-mode` resolves:

`https://dbyiw3u3rf9yr.cloudfront.net/corpora/openobserve_1b/documents-1k.json.gz`

The full manifest is:

`https://dbyiw3u3rf9yr.cloudfront.net/corpora/openobserve_1b/v1/manifest.json`

It records exactly 1,000,000,000 rows, 100 chunk SHA-256 values, timestamp bounds,
743,267,587,646 compressed bytes, and 2,238,012,424,292 uncompressed bytes. The
1,000-document test-mode corpus remains published at the URL above; its committed
manifest records all query anchors and q01-q19 goldens.

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
