#!/bin/bash
# Drive the otel_dp OSB workload (OVI3D0/opensearch-benchmark-workloads@mustang) to a primary-size
# target as a near-continuous stream of fixed-length chunks. Each chunk is its own OSB test
# execution, so the shared benchmark datastore gets a full results doc set (throughput +
# service-time percentiles) per chunk, plus continuous node-stats telemetry.
#
# Fresh data: while a chunk ingests (looping its corpus), the next corpus is generated in the
# background with a new seed (niced, so OSB keeps priority). At the chunk boundary the loop
# switches to it and deletes the old one. The generator also writes OSB's .offset tables, so the
# gap between chunks is just OSB start-up.
#
# Run ON the load-generator host under setsid (outlives SSM):
#   EP=https://search-... AUTH=user:pass setsid nohup bash run_otel_dp_osb.sh > /opt/otel/osb-loop.log 2>&1 &
#
# Env knobs: EP, AUTH (required), CORPUS_DIR (initial corpus), TARGET_BYTES, DISK_STOP,
#            CHUNK_SECONDS, CLIENTS, MAX_CHUNKS (0 = until target), SKIP_SETUP=1 (resume without
#            wiping), ROTATE=0 (never regenerate), GEN_SPANS, GEN_PROCS, RUN_TYPE, DOMAIN_TAG,
#            RUNNER_TAG (distinguishes parallel runners in the datastore; default hostname),
#            VARIANT (mustang|baseline tag), EXTRA_PARAMS (extra workload params, e.g. refresh_interval:30s),
#            WAIT_FOR_SETUP=1 (with SKIP_SETUP=1: wait until another generator's setup has finished).
# Stop cleanly at the next chunk boundary: touch /opt/otel/STOP
set -u
: "${EP:?set EP=https://<domain endpoint>}"
: "${AUTH:?set AUTH=<user>:<password> for the target domain}"
CORPUS_DIR=${CORPUS_DIR:-/opt/otel/corpus}
CORPORA=${CORPORA:-/opt/otel/corpora}
TARGET_BYTES=${TARGET_BYTES:-10995116277760}   # 10 TiB primary across logs + spans
DISK_STOP=${DISK_STOP:-80}                      # max data-node disk.percent
CHUNK_SECONDS=${CHUNK_SECONDS:-3600}
CLIENTS=${CLIENTS:-48}                          # per signal (logs, spans)
MAX_CHUNKS=${MAX_CHUNKS:-0}
ROTATE=${ROTATE:-1}
GEN_SPANS=${GEN_SPANS:-100000000}               # ~100M spans + ~100M logs ≈ 267 GB per corpus
GEN_PROCS=${GEN_PROCS:-24}
GEN_MIN_FREE_GB=${GEN_MIN_FREE_GB:-320}
RUN_TYPE=${RUN_TYPE:-otel-dp-ingest}
DOMAIN_TAG=${DOMAIN_TAG:-$(echo "${EP#https://}" | sed -E 's/^search-//; s/-[a-z0-9]{26}\..*//')}  # AOS domain name
RUNNER_TAG=${RUNNER_TAG:-$(hostname -s)}
VARIANT=${VARIANT:-mustang}
EXTRA_PARAMS=${EXTRA_PARAMS:-}
RESULTS_DIR=${RESULTS_DIR:-/opt/otel/results}
GEN=${GEN:-$(cd "$(dirname "$0")" && pwd)/otel_dp_ingest.py}
DATA=/root/.benchmark/benchmarks/data
export HOME=/root PATH=/usr/local/bin:$PATH
log() { echo "$(date -u +%FT%TZ) $*"; }

mkdir -p "$RESULTS_DIR" "$CORPORA" "$DATA"/otel_dp_logs "$DATA"/otel_dp_spans "$DATA"/otel_dp_service_map

# OSB checks out the local `mustang` branch without pulling — force it to origin/mustang.
WL=/root/.benchmark/benchmarks/workloads/ovi
if [ -d "$WL/.git" ]; then
  git -C "$WL" fetch -q --prune origin '+refs/heads/mustang:refs/remotes/origin/mustang' &&
    git -C "$WL" checkout -q -f -B mustang origin/mustang && git -C "$WL" clean -qfd
  log "workload revision: $(git -C "$WL" log --oneline -1)"
fi

use_corpus() {  # point OSB's data dir at corpus dir $1 and load its doc counts
  local d=$1 f
  for f in otel_dp_logs/logs.json otel_dp_spans/spans.json otel_dp_service_map/service-map.json; do
    ln -sfn "$d/${f#*/}" "$DATA/$f"
    # Pre-built offset table if the generator wrote one; otherwise keep OSB's own (initial corpus).
    if [ -f "$d/${f#*/}.offset" ]; then ln -sfn "$d/${f#*/}.offset" "$DATA/$f.offset"; fi
  done
  counts=$(python3.11 -c "import json;m=json.load(open('$d/corpus-meta.json'));print(f\"logs_document_count:{m['logs_document_count']},spans_document_count:{m['spans_document_count']},service_map_document_count:{m['service_map_document_count']}\")")
  CURRENT=$d
  log "corpus: $d ($counts)"
}

ready_corpora() {  # finished rotated corpora other than the current one, oldest first
  ls -d "$CORPORA"/c-* 2>/dev/null | grep -v '\.tmp$' | grep -vx "$CURRENT" | sort
}

GEN_PID=
start_generation() {  # background-generate the next corpus unless one is pending or disk is short
  [ "$ROTATE" = 1 ] || return 0
  [ -n "$GEN_PID" ] && kill -0 "$GEN_PID" 2>/dev/null && return 0
  [ -n "$(ready_corpora)" ] && return 0
  local free; free=$(df --output=avail -BG / | tail -1 | tr -dc 0-9)
  [ "$free" -ge "$GEN_MIN_FREE_GB" ] || { log "generation skipped: ${free}G free < ${GEN_MIN_FREE_GB}G"; return 0; }
  local seed; seed=$(date +%s); local out="$CORPORA/c-$seed"
  rm -rf "$CORPORA"/c-*.tmp
  ( nice -n 19 ionice -c3 python3.11 "$GEN" corpus --out-dir "$out.tmp" --spans "$GEN_SPANS" \
      --procs "$GEN_PROCS" --seed "$seed" > "$CORPORA/gen-$seed.log" 2>&1 && mv "$out.tmp" "$out" ) &
  GEN_PID=$!
  log "generating next corpus c-$seed (pid $GEN_PID)"
}

rotate_corpus() {  # at a chunk boundary: switch to the newest ready corpus, drop the old one
  [ "$ROTATE" = 1 ] || return 0
  local next; next=$(ready_corpora | tail -1)
  [ -n "$next" ] || { log "no new corpus ready yet — reusing $CURRENT"; return 0; }
  local old=$CURRENT
  use_corpus "$next"
  rm -rf "$old"
  log "rotated corpus: deleted $old"
}

state() {  # prints "<primary bytes> <max disk %>"
  python3.11 - "$EP" "$AUTH" <<'PY'
import base64, json, ssl, sys, urllib.request
ep, auth = sys.argv[1], "Basic " + base64.b64encode(sys.argv[2].encode()).decode()
ctx = ssl._create_unverified_context()
def get(p):
    return json.load(urllib.request.urlopen(urllib.request.Request(ep + p, headers={"Authorization": auth}), timeout=60, context=ctx))
try:
    pri = sum(int(r["pri.store.size"] or 0) for r in get("/_cat/indices/logs-otel-v1-*,otel-v1-apm-span-*?format=json&bytes=b&h=pri.store.size"))
    disk = max(int(r["disk.percent"]) for r in get("/_cat/allocation?format=json&h=disk.percent") if r.get("disk.percent"))
    print(pri, disk)
except Exception as e:
    print(-1, -1)
PY
}

# max_retries:0 — a hung bulk fails once at the timeout instead of being retried for minutes,
# which otherwise stalls the end of every time-period chunk on a few straggler clients.
osb() {  # $1 = test procedure, $2 = chunk tag, $3 = extra workload params
  opensearch-benchmark execute-test \
    --pipeline=benchmark-only \
    --target-hosts="${EP#https://}:443" \
    --client-options="use_ssl:true,verify_certs:false,basic_auth_user:'${AUTH%%:*}',basic_auth_password:'${AUTH#*:}',timeout:120,max_retries:0" \
    --workload-repository=ovi --workload=otel_dp --workload-revision=mustang \
    --test-procedure="$1" \
    --workload-params="${counts},bulk_indexing_clients:${CLIENTS}${EXTRA_PARAMS:+,$EXTRA_PARAMS}${3:+,$3}" \
    --telemetry=node-stats --telemetry-params="node-stats-sample-interval:30" \
    --user-tag="run-type:${RUN_TYPE},domain:${DOMAIN_TAG},variant:${VARIANT},dataset:otel-dp,procedure:$1,chunk:$2,corpus:$(basename "$CURRENT"),runner:${RUNNER_TAG}" \
    --results-format=csv --results-file="$RESULTS_DIR/$2.csv" \
    --on-error="${ON_ERROR:-continue}" \
    --kill-running-processes   # dedicated runner: clears OSB leftovers from an interrupted chunk
}

# Initial corpus: newest finished rotated corpus if resuming, else CORPUS_DIR.
init=$(ls -d "$CORPORA"/c-* 2>/dev/null | grep -v '\.tmp$' | sort | tail -1)
use_corpus "${init:-$CORPUS_DIR}"
start_generation

# Secondary generator: wait for the primary generator's setup (service map is its last step).
if [ "${SKIP_SETUP:-0}" = 1 ] && [ "${WAIT_FOR_SETUP:-0}" = 1 ]; then
  until curl -sfk -m 30 -u "$AUTH" "$EP/otel-v2-apm-service-map/_count" | grep -q '"count":[1-9]' &&
        curl -sfk -m 30 -u "$AUTH" "$EP/_alias/logs-otel-v1,otel-v1-apm-span" > /dev/null; do
    log "waiting for setup on $DOMAIN_TAG"; sleep 30
  done
  sleep 30
fi

# Resuming over a chunk started by a previous loop: let it finish rather than kill it.
while pgrep -f "opensearch-benchmark execute-test" > /dev/null; do sleep 30; done

# Disk watchdog: a chunk can run an hour — kill OSB mid-chunk if any data node crosses DISK_STOP+5.
( while sleep 60; do read -r _ d < <(state); [ "${d:--1}" -ge $((DISK_STOP + 5)) ] && {
    log "WATCHDOG disk ${d}% — killing OSB"; pkill -TERM -f 'opensearch-benchmark execute-test'; touch /opt/otel/STOP; }; done ) &
WATCHDOG=$!
trap 'kill $WATCHDOG 2>/dev/null; [ -n "$GEN_PID" ] && kill "$GEN_PID" 2>/dev/null' EXIT
rm -f /opt/otel/STOP

if [ "${SKIP_SETUP:-0}" != 1 ]; then
  log "=== setup ==="
  ON_ERROR=abort osb setup setup "" || { echo "setup failed"; exit 1; }
  # --on-error=continue would hide a failed template PUT and leave dynamically-mapped indices.
  python3.11 - "$EP" "$AUTH" <<'PY' || { echo "setup verification failed"; exit 1; }
import base64, json, ssl, sys, urllib.request
ep, auth = sys.argv[1], "Basic " + base64.b64encode(sys.argv[2].encode()).decode()
ctx = ssl._create_unverified_context()
get = lambda p: json.load(urllib.request.urlopen(urllib.request.Request(ep + p, headers={"Authorization": auth}), timeout=60, context=ctx))
for idx, field in (("logs-otel-v1-000001", "severity"), ("otel-v1-apm-span-000001", "durationInNanos")):
    props = next(iter(get(f"/{idx}/_mapping").values()))["mappings"].get("properties", {})
    assert field in props, f"{idx} missing template mapping ({field})"
print("setup verified: Data Prepper mappings in place")
PY
fi

# Continue after the highest chunk number (failed chunks leave gaps, so a file count would collide).
n=$(ls "$RESULTS_DIR" 2>/dev/null | sed -nE 's/^chunk-0*([0-9]+)\.csv$/\1/p' | sort -n | tail -1); n=${n:-0}
ran=0
while :; do
  read -r pri disk < <(state)
  log "primary=$(python3.11 -c "print(f'{$pri/2**40:.3f}')") TiB max_disk=${disk}%"
  [ -f /opt/otel/STOP ] && { echo "STOP file present — exiting"; break; }
  [ "$pri" -ge "$TARGET_BYTES" ] && { echo "target reached — done"; break; }
  [ "$disk" -ge "$DISK_STOP" ] && { echo "disk guard ${disk}% — done"; break; }
  [ "$MAX_CHUNKS" -gt 0 ] && [ "$ran" -ge "$MAX_CHUNKS" ] && { echo "MAX_CHUNKS reached"; break; }
  if [ "$ran" -gt 0 ] || [ "$n" -gt 0 ]; then rotate_corpus; fi
  start_generation
  ran=$((ran + 1)); n=$((n + 1)); tag=$(printf 'chunk-%03d' "$n")
  log "=== $tag (${CHUNK_SECONDS}s, ${CLIENTS} clients/signal) ==="
  osb ingest "$tag" "time_period:${CHUNK_SECONDS}" || echo "$tag: OSB exited non-zero"
done
