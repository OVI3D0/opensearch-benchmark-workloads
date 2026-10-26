# otel_dashboard — dashboard widget queries over OTel logs + traces

Search-only workload. It runs the queries a **logs dashboard** and an **APM / traces dashboard** issue, over data in
the Data Prepper standard index shape (`logs-otel-v1-*`, `otel-v1-apm-span-*`, `otel-v2-apm-service-map`) — for
example data loaded with the [`otel_dp`](../otel_dp) workload or a Data Prepper pipeline. It creates no indices
and loads no corpus: point it at a cluster that already holds the data, after ingest has stopped (or during
ingest, if that is what you want to measure).

## Queries (`operations/ppl.json`, 27 PPL ops)

| Logs dashboard | APM / traces dashboard |
|---|---|
| `dash-l01-log-volume` · log volume over time | `dash-t01-request-rate` · server spans over time |
| `dash-l02-volume-by-severity` · stacked by severity | `dash-t02-latency-percentiles` · p50 / p95 / p99 over time |
| `dash-l03-top-error-services` · top 10 services by errors | `dash-t03-error-rate` · errors / total over time |
| `dash-l04-error-rate-by-service` · error % by service (eval) | `dash-t04-red-by-service` · rate, errors, p95 by service (eval) |
| `dash-l04b-error-rate-by-service-split` · same, without eval | `dash-t04b-red-by-service-split` · same, without eval |
| `dash-l05-top-exception-types` | `dash-t05-slowest-routes` · p95 by `http.route` |
| `dash-l06-errors-by-cluster-namespace` | `dash-t06-http-status` · status code mix |
| `dash-l07-logs-by-env-region` | `dash-t07-error-types` · by service |
| `dash-l08-error-pods-by-service` · distinct pods | `dash-t08-entry-points` · trace groups + p95 |
| `dash-l09-latest-errors` · newest 50 error lines | `dash-t09-db-calls` · by db system / collection |
| `dash-l10-fts-timeout-by-service` · full-text `match` | `dash-t10-kafka-topics` · producer spans by topic |
| `dash-l11-logs-for-trace` · drill-down | `dash-t11-slowest-traces` · slowest 20 root spans |
| | `dash-t12-distinct-traces-users` · `dc()` |
| | `dash-t13-trace-waterfall` · drill-down |
| | `dash-t14-service-map` · service → service edges |

Conventions: errors are `severity.number >= 17` (severity text spelling varies by SDK language); latency widgets use
percentiles, never `avg()` on nanosecond fields (it can overflow a long sum on large windows). `l04` / `t04` compute
an error flag with `eval` and sum it per service; `l04b` / `t04b` return the same information without `eval`
(per-service counts split by severity / status code) for engines where the `eval` form is slow.

## Parameters

| Param | Default | Meaning |
|---|---|---|
| `time_from` | unset (= all time) | window start `'YYYY-MM-DD HH:MM:SS'`; adds `where @timestamp >= time_from` to every logs/spans query |
| `time_to` | unset | optional window end (`and @timestamp < time_to`) |
| `bucket` | `1m` | time-series bucket for the `span()` widgets — pick one per window (table below) |
| `trace_id` | zeros | trace for the two drill-downs; pick one inside the window that has logs |
| `logs_index` / `spans_index` / `service_map_index` | the three patterns above | |
| `logs_time_field` / `spans_time_field` | `@timestamp` | |
| `warmup_iterations` / `iterations` | 1 / 3 | `dashboard` procedure |
| `heavy_warmup_iterations` / `heavy_iterations` | 0 / 1 | `l04`, `t04` in the `dashboard` procedure |
| `dashboard_users` / `dashboard_loads` / `load_warmup_iterations` | 1 / 5 / 1 | `*-dashboard-load` procedures |

Window → bucket (keeps every chart at ~50–300 points):

| Window | 1 min | 5 min | 15 min | 30 min | 1 h | 4 h | 6 h | 8 h | 24 h | 7 d | 14 d | 30 d | all |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `bucket` | 1s | 5s | 10s | 30s | 1m | 5m | 5m | 5m | 15m | 1h | 3h | 6h | 1d |

## Test procedures

- `dashboard` (default) — every query, sequentially, one client.
- `logs-dashboard-load` — the 9 logs-dashboard widgets (`l01, l02, l03, l04b, l05, l06, l07, l09, l10`) fired
  **concurrently**, one OSB task per widget with `dashboard_users` clients each.
- `apm-dashboard-load` — the 9 APM widgets (`t01, t02, t03, t04b, t05, t06, t07, t08, t11`) fired concurrently.

OSB reports per-widget service time under that concurrency; a dashboard load takes about as long as its slowest
widget (OSB doesn't synchronise the widgets into discrete "loads").

## Running it

```bash
# one window: last 1 h of data that ends at 2026-10-26 19:50:00
opensearch-benchmark execute-test --pipeline=benchmark-only --target-hosts=https://<host>:443 \
  --client-options="use_ssl:true,verify_certs:false,basic_auth_user:'<user>',basic_auth_password:'<password>',timeout:600,max_retries:0" \
  --workload-repository=<repo> --workload=otel_dashboard --test-procedure=dashboard \
  --workload-params='{"time_from":"2026-10-26 18:50:00","bucket":"1m","trace_id":"<trace inside the window>"}'

# concurrent logs dashboard, 10 users
... --test-procedure=logs-dashboard-load --workload-params='{"time_from":"...","bucket":"1m","dashboard_users":10}'
```

Run one execution per window (smallest first) and anchor every window at the end of the data, so each window
holds the newest data. For fair engine comparisons turn the shard request cache off on the target indices
(`index.requests.cache.enable: false`) so repeated iterations recompute instead of returning cached results.
