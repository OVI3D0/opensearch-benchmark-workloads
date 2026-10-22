## otel_logs: OTel Demo workload (spans, logs, metrics)

This replaces the earlier synthetic single-index `otel_logs` workload (still in this branch's history).

Telemetry from the [OpenTelemetry Demo](https://opentelemetry.io/docs/demo/) application, exported
through an OTel collector and Data Prepper into the OpenSearch OTel format (dotted keys such as
`resource.attributes.service@name`). Three indices, ingested from one corpus with three files:

| index | documents |
|---|---|
| `otel_spans` | 36,987,718 |
| `otel_logs` | 13,148,738 |
| `otel_metrics` | 4,547,738 |

The corpus is not published with the workload: place the files from `files.txt` in the
benchmark data directory (`<data root>/otel_demo/`) or pass `corpus_base_url`.

### Index bodies

`index/<index>.json`. By default each index is created with the composite data format
(parquet primary, lucene secondary) and `multi_value` multi-valued fields; pass
`parquet_enabled_index:false` for plain Lucene indices with the same mapping. Index sorting is on
`eventTime`; dates are `date_nanos`; spans `events`/`links` and metrics `buckets`/`exemplars` are
`nested`; attributes are dynamic keywords.

### Queries

45 PPL queries in `operations/default.json`, named after their ids: `t1-*` (19 real-use
observability queries over traces, logs and metrics) and `t2-*` (26 engine stress queries). Run
with `plugins.ppl.syntax.legacy.preferred=true` (the `legacy_syntax` parameter, default true,
set by the first operation of the PPL schedule).

The same queries are provided for other engines, in schedule order, each checked to return the
same result as PPL on a sample of the corpus:

* `operations/dsl.json`: native OpenSearch DSL (`/_search`) for plain Lucene indices
  (test procedure `dsl-otel-demo`).
* `clickhouse/create.sql` + `operations/clickhouse.json`, `datafusion/queries.sql`,
  `openobserve/queries.sql`: SQL over the corpus converted to typed tables (one column per field,
  nested fields as arrays of structs). A line `-- SKIP: <reason>` holds the place of a query an
  engine cannot express.

### Test procedures

* `otel-demo` (default): create the indices, ingest, run the PPL queries.
* `otel-demo-test`: the same with one iteration per query.
* `otel-demo-ingest`: ingest only. `otel-demo-ppl`: PPL queries only. `dsl-otel-demo`: DSL queries only.

### Parameters

`bulk_size` (5000), `bulk_indexing_clients` (8), `ingest_percentage` (100), `spans_index`,
`logs_index`, `metrics_index` (index names), `number_of_shards` (1), `number_of_replicas` (0),
`parquet_enabled_index` (true), `legacy_syntax` (true, a boolean), `warmup_iterations` (10),
`test_iterations` (10), `target_throughput` (2), `search_clients` (1), and per query
`<query_name_with_underscores>_iterations` etc.
