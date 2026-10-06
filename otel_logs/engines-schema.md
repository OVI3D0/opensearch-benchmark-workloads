# Flattened Columnar Schema — OTel Logs (shared by ClickHouse / DataFusion / OpenObserve)

The canonical PPL/Mustang side (`workloads/otel_logs/operations/default.json`) queries an
OpenSearch index with **nested** OTel field names (`resource.cloud.region`,
`log.http.status_code`, …) and dotted paths quoted with backticks. ClickHouse, Apache
DataFusion and OpenObserve all query a **flat, columnar** table/stream, so every nested
path the 39 queries touch must be mapped to a single flat column. This file is the one
source of truth for that mapping; all three SQL query sets reference exactly these names.

## Design decisions

1. **Separator = underscore, dotted path joined left-to-right.**
   `resource.cloud.region` → `resource_cloud_region`. This matches OpenObserve's native
   nested-JSON flattening (it replaces `.` with `_`), so the same physical column names
   work for OpenObserve's auto-ingested streams and for the ClickHouse/DataFusion tables
   the loader builds. (Verify the exact OpenObserve ingest separator at load time — see
   the note in VERIFICATION_REPORT.md; it does not change query authoring because all
   three sets simply reference the agreed names below.)

2. **All identifiers lowercase `snake_case`, including the top-level camelCase OTel
   fields** (`serviceName` → `service_name`, `severityText` → `severity_text`,
   `severityNumber` → `severity_number`, `traceId` → `trace_id`). Rationale: ClickHouse
   identifiers are case-sensitive; DataFusion folds unquoted identifiers to lowercase;
   OpenObserve lowercases. A single lowercase snake_case convention lets the *identical*
   column tokens parse unquoted on all three engines with no per-engine casing/quoting.

3. **`time` stays a raw epoch-millis integer (BIGINT), not a timestamp.** The PPL side
   stores `time` as a `long` (epoch ms) and buckets with `span(time, 10000)` /
   `span(time, 60000)` — i.e. numeric floor bucketing, *not* calendar bucketing. The
   faithful, deterministic translation is integer arithmetic on the millis value
   (`intDiv`/integer `/`), **not** `date_bin`/`toStartOfInterval`/`histogram`, which all
   require a real timestamp type and would need a cast + risk calendar/zone drift. See
   q13/q14 notes in VERIFICATION_REPORT.md.

4. **Table / stream name = `otel_logs`** on every engine (matches the PPL
   `index_name` default). OpenObserve references it double-quoted (`FROM "otel_logs"`);
   ClickHouse and DataFusion use it bare.

## Full corpus schema

The frozen corpus (`scripts/nightly-perf/otel_corpus.py`, `FIELDS`) carries **47
columns** — every leaf of the Mustang OTel mapping except `observedTimestamp` — so all
engines load the same full record (fair ingest throughput / size). `log.db.*` and
`log.exception.*` are NULL when absent. `clickhouse/create.sql` is generated from it
(`otel_corpus.py ddl`); the table below lists the columns the 39 queries touch.

## Column mapping (every field referenced by q01–q39)

| PPL nested field (backticked)        | Flat column                        | Type        | Used by |
|--------------------------------------|------------------------------------|-------------|---------|
| `time`                               | `time`                             | BIGINT (ms) | q11,q13,q14,q35,q36 |
| `severityText`                       | `severity_text`                    | String      | q02,q05,q10,q20,q31,q32,q36 |
| `severityNumber`                     | `severity_number`                  | BIGINT      | q11,q15,q16,q17,q23,q26,q28,q29,q34 |
| `serviceName`                        | `service_name`                     | String      | q03,q10,q12,q15,q16,q17,q18,q19,q21,q22,q23,q24,q27,q28,q30,q31,q33,q35,q36,q37,q38,q39 |
| `traceId`                            | `trace_id`                         | String      | q18 |
| `resource.cloud.region`              | `resource_cloud_region`            | String      | q04,q39 |
| `resource.k8s.namespace.name`        | `resource_k8s_namespace_name`      | String      | q08,q27 |
| `resource.k8s.pod.name`              | `resource_k8s_pod_name`            | String      | q12 |
| `resource.host.name`                 | `resource_host_name`               | String      | q12 |
| `resource.telemetry.sdk.language`    | `resource_telemetry_sdk_language`  | String      | q09,q17 |
| `log.http.method`                    | `log_http_method`                  | String      | q06 |
| `log.http.status_code`               | `log_http_status_code`             | BIGINT      | q06,q07,q11,q24,q25,q29,q34 |

Per-engine physical types (used in `clickhouse/create.sql`):
- String columns → ClickHouse `String`, DataFusion/Arrow `Utf8`, OpenObserve `Utf8` (auto).
- `time`, `severity_number`, `log_http_status_code` → ClickHouse `Int64`, DataFusion `Int64`.

> NOTE on the OpenObserve `_timestamp` column: OpenObserve injects its own
> microsecond `_timestamp` on ingest and the search API filters on it. The queries here
> deliberately bucket/sort on the data's own `time` column (to match PPL `span(time,…)`
> and `sort - time`), **not** `_timestamp`. The driver already passes a wide
> `_timestamp` window so no rows are excluded.
