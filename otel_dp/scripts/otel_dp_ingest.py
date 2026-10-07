#!/usr/bin/env python3
"""Full-speed OTel logs + traces ingest in the Data Prepper "standard" index shape.

Unlike otel-log-generator.py (flat 48-field logs only), this writes documents shaped
exactly like Data Prepper's otel_logs / otel_traces sinks emit, into indices created
from Data Prepper's own templates:

    logs-otel-v1-index-standard-template.json   -> alias logs-otel-v1      (logs-otel-v1-000001, ...)
    otel-v1-apm-span-index-standard-template.json -> alias otel-v1-apm-span (otel-v1-apm-span-000001, ...)
    otel-v2-apm-service-map-index-template.json -> index otel-v2-apm-service-map

One deviation from stock Data Prepper, required on composite (Mustang) indices: the
span template gets keyword dynamic templates for events.attributes.* and
links.attributes.*. Stock DP leaves those to default dynamic mapping (text +
.keyword), and composite rejects `text` under `nested` ("fields there cannot be
searched. Use [keyword] instead.") -> every span with a string event attribute fails.

Docs are correlated like real OTel Demo traffic: each synthetic trace fans out over
3-8 spans across the demo services, and logs carry the traceId/spanId of a span in
that trace. Stdlib only (runs on a bare AL2023 python3.11).

Usage (run ON the load-generator host, under setsid so it outlives SSM):
    python3 otel_dp_ingest.py setup --endpoint https://host --username U --password P --shards 10 --replicas 1
    python3 otel_dp_ingest.py run   --endpoint https://host --username U --password P --procs 48 \
        --target-bytes $((10 * 2**40))      # stop at 10 TiB primary (logs + spans)
"""
import argparse
import base64
import http.client
import json
import multiprocessing as mp
import os
import random
import signal
import ssl
import sys
import time
import urllib.parse
import urllib.request

DP_RAW = ("https://raw.githubusercontent.com/opensearch-project/data-prepper/main/"
          "data-prepper-plugins/opensearch/src/main/resources/index-template/")
LOGS_ALIAS = "logs-otel-v1"
SPAN_ALIAS = "otel-v1-apm-span"
SERVICE_MAP_INDEX = "otel-v2-apm-service-map"
ISM_POLICY = "otel-dp-rollover"
DISK_STOP_PERCENT = 80

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


# ---------------------------------------------------------------- HTTP helpers

def _auth(user, pw):
    return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()


def req(endpoint, auth, method, path, body=None, timeout=60):
    data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    r = urllib.request.Request(endpoint + path, data=data, method=method,
                               headers={"Authorization": auth, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=timeout, context=CTX) as resp:
            txt = resp.read().decode()
            return resp.status, (json.loads(txt) if txt.strip().startswith(("{", "[")) else txt)
    except urllib.error.HTTPError as e:
        txt = e.read().decode()
        try:
            return e.code, json.loads(txt)
        except ValueError:
            return e.code, txt


# ---------------------------------------------------------------- setup

def dp_template(name):
    with urllib.request.urlopen(DP_RAW + name, timeout=30) as resp:
        return json.load(resp)["template"]["mappings"]


def cmd_setup(a):
    auth = _auth(a.username, a.password)
    base_settings = {"number_of_shards": a.shards, "number_of_replicas": a.replicas,
                     "mapping.total_fields.limit": 10000}

    span_mappings = dp_template("otel-v1-apm-span-index-standard-template.json")
    span_mappings["dynamic_templates"] = [
        {"string_event_attributes": {"path_match": "events.attributes.*", "match_mapping_type": "string",
                                     "mapping": {"type": "keyword", "ignore_above": 256}}},
        {"string_link_attributes": {"path_match": "links.attributes.*", "match_mapping_type": "string",
                                    "mapping": {"type": "keyword", "ignore_above": 256}}},
    ] + span_mappings.get("dynamic_templates", [])

    templates = {
        LOGS_ALIAS: (dp_template("logs-otel-v1-index-standard-template.json"), LOGS_ALIAS),
        SPAN_ALIAS: (span_mappings, SPAN_ALIAS),
        SERVICE_MAP_INDEX: (dp_template("otel-v2-apm-service-map-index-template.json"), None),
    }
    for name, (mappings, alias) in templates.items():
        settings = dict(base_settings)
        if alias:
            settings["plugins.index_state_management.rollover_alias"] = alias
        else:
            settings["number_of_shards"] = 1  # service map is tiny
        body = {"index_patterns": [f"{name}-*" if alias else name], "priority": 200,
                "template": {"settings": settings, "mappings": mappings}}
        print(f"PUT _index_template/{name}:", req(a.endpoint, auth, "PUT", f"/_index_template/{name}", body))

    # Data Prepper's own convention: ISM rollover on the write alias (DP uses 50gb / 24h).
    policy = {"policy": {
        "description": "Data Prepper style rollover for otel logs + spans",
        "default_state": "current_write_index",
        "states": [{"name": "current_write_index",
                    "actions": [{"rollover": {"min_primary_shard_size": a.rollover_shard_size}}],
                    "transitions": []}],
        "ism_template": [{"index_patterns": [f"{LOGS_ALIAS}-*", f"{SPAN_ALIAS}-*"], "priority": 100}]}}
    st, existing = req(a.endpoint, auth, "GET", f"/_plugins/_ism/policies/{ISM_POLICY}")
    if st == 200:
        q = f"?if_seq_no={existing['_seq_no']}&if_primary_term={existing['_primary_term']}"
        print("ISM policy (update):", req(a.endpoint, auth, "PUT", f"/_plugins/_ism/policies/{ISM_POLICY}{q}", policy))
    else:
        print("ISM policy (create):", req(a.endpoint, auth, "PUT", f"/_plugins/_ism/policies/{ISM_POLICY}", policy))

    for alias in (LOGS_ALIAS, SPAN_ALIAS):
        st, _ = req(a.endpoint, auth, "GET", f"/_alias/{alias}")
        if st == 200:
            print(f"alias {alias} already exists — leaving it")
            continue
        body = {"aliases": {alias: {"is_write_index": True}}}
        print(f"PUT {alias}-000001:", req(a.endpoint, auth, "PUT", f"/{alias}-000001", body))
    st, _ = req(a.endpoint, auth, "HEAD", f"/{SERVICE_MAP_INDEX}")
    if st != 200:
        print(f"PUT {SERVICE_MAP_INDEX}:", req(a.endpoint, auth, "PUT", f"/{SERVICE_MAP_INDEX}", {}))


# ---------------------------------------------------------------- doc generation

# OTel Demo service graph: service -> (span kind, operations, downstream services)
SERVICES = {
    "frontend-proxy": ("SPAN_KIND_SERVER", ["ingress", "router frontend egress"], ["frontend"]),
    "frontend": ("SPAN_KIND_SERVER", ["GET /", "GET /api/products", "POST /api/cart", "POST /api/checkout",
                                      "GET /api/recommendations"], ["product-catalog", "cart", "checkout",
                                                                    "recommendation", "ad", "currency"]),
    "product-catalog": ("SPAN_KIND_SERVER", ["oteldemo.ProductCatalogService/GetProduct",
                                             "oteldemo.ProductCatalogService/ListProducts"], []),
    "cart": ("SPAN_KIND_SERVER", ["oteldemo.CartService/AddItem", "oteldemo.CartService/GetCart",
                                  "oteldemo.CartService/EmptyCart"], ["valkey-cart"]),
    "valkey-cart": ("SPAN_KIND_CLIENT", ["HGET", "HMSET", "EXPIRE"], []),
    "checkout": ("SPAN_KIND_SERVER", ["oteldemo.CheckoutService/PlaceOrder"],
                 ["cart", "payment", "shipping", "currency", "email", "kafka"]),
    "payment": ("SPAN_KIND_SERVER", ["oteldemo.PaymentService/Charge"], []),
    "shipping": ("SPAN_KIND_SERVER", ["oteldemo.ShippingService/GetQuote", "oteldemo.ShippingService/ShipOrder"],
                 ["quote"]),
    "quote": ("SPAN_KIND_SERVER", ["POST /getquote"], []),
    "currency": ("SPAN_KIND_SERVER", ["oteldemo.CurrencyService/Convert",
                                      "oteldemo.CurrencyService/GetSupportedCurrencies"], []),
    "email": ("SPAN_KIND_SERVER", ["POST /send_order_confirmation"], []),
    "recommendation": ("SPAN_KIND_SERVER", ["oteldemo.RecommendationService/ListRecommendations"],
                       ["product-catalog"]),
    "ad": ("SPAN_KIND_SERVER", ["oteldemo.AdService/GetAds"], []),
    "kafka": ("SPAN_KIND_PRODUCER", ["orders publish"], ["accounting", "fraud-detection"]),
    "accounting": ("SPAN_KIND_CONSUMER", ["orders process"], []),
    "fraud-detection": ("SPAN_KIND_CONSUMER", ["orders process"], []),
}
LANG = {"frontend": "nodejs", "frontend-proxy": "cpp", "product-catalog": "go", "cart": "dotnet",
        "valkey-cart": "dotnet", "checkout": "go", "payment": "nodejs", "shipping": "rust", "quote": "php",
        "currency": "cpp", "email": "ruby", "recommendation": "python", "ad": "java", "kafka": "go",
        "accounting": "dotnet", "fraud-detection": "kotlin"}
PRODUCTS = ["OLJCESPC7Z", "66VCHSJNUP", "1YMWWN1N4O", "L9ECAV7KIM", "2ZYFJ3GM2N", "0PUK6V6EV0",
            "LS4PSXUNUM", "9SIQT8TOJO", "6E92ZMYYFZ", "HQTGWGPNH4"]
CURRENCIES = ["USD", "EUR", "CAD", "JPY", "GBP", "TRY", "INR", "BRL"]
ZONES = ["eu-west-1a", "eu-west-1b", "eu-west-1c"]
LOG_TEMPLATES = [
    (9, "INFO", "Received request {op} for user {user}"),
    (9, "INFO", "{op} completed in {ms} ms with status {code}"),
    (9, "INFO", "Product {product} added to cart {cart} quantity {qty}"),
    (9, "INFO", "Order {order} placed successfully total {amount} {cur}"),
    (9, "INFO", "Converting {amount} from USD to {cur}"),
    (5, "DEBUG", "cache lookup key=cart:{cart} hit={hit} ttl={ttl}s"),
    (5, "DEBUG", "grpc call {op} peer={peer} deadline={ms}ms"),
    (13, "WARN", "Slow downstream call to {peer}: {ms} ms exceeds budget"),
    (13, "WARN", "Retrying {op} attempt {qty} after transient error: connection reset by peer"),
    (17, "ERROR", "Payment request failed: card declined for user {user} order {order}"),
    (17, "ERROR", "Failed to get product {product}: rpc error code = Unavailable desc = upstream connect error"),
    (17, "ERROR", "java.lang.IllegalStateException: Ad service overloaded, dropping request {op}\n"
                  "\tat oteldemo.AdService.getAds(AdService.java:{qty}1)\n\tat io.grpc.ServerCalls.invoke"),
]
LOG_WEIGHTS = [30, 25, 12, 6, 6, 8, 5, 3, 2, 1, 1, 1]


def _hex(n, rnd):
    return "%0*x" % (n * 2, rnd.getrandbits(n * 8))


def _iso(ns):
    s, frac = divmod(ns, 1_000_000_000)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(s)) + ".%09dZ" % frac


def _resource(svc, pod):
    return {"attributes": {
        "service.name": svc, "service.namespace": "opentelemetry-demo", "service.version": "2.0.2",
        "service.instance.id": f"{svc}-{pod}", "telemetry.sdk.language": LANG[svc],
        "telemetry.sdk.name": "opentelemetry", "telemetry.sdk.version": "1.30.0",
        "host.name": f"ip-10-0-{pod % 64}-{pod % 250}.eu-west-1.compute.internal",
        "k8s.namespace.name": "otel-demo", "k8s.pod.name": f"{svc}-7d9f8c{pod:04d}",
        "k8s.node.name": f"node-{pod % 24}", "cloud.provider": "aws", "cloud.region": "eu-west-1",
        "cloud.availability_zone": ZONES[pod % 3]}, "droppedAttributesCount": 0, "schemaUrl": ""}


def _scope(svc):
    return {"name": f"opentelemetry.instrumentation.{LANG[svc]}", "version": "0.52b0",
            "schemaUrl": "", "droppedAttributesCount": 0}


def gen_trace(rnd, now_ns):
    """Return (spans, logs) for one synthetic trace."""
    trace_id = _hex(16, rnd)
    root_op = rnd.choice(SERVICES["frontend"][1])
    error_trace = rnd.random() < 0.03
    total_ns = rnd.randint(2, 900) * 1_000_000
    start_ns = now_ns - total_ns - rnd.randint(0, 2_000_000_000)

    # Walk the service graph breadth-first from the proxy, bounded to 3-8 spans.
    want = rnd.randint(3, 8)
    queue = [("frontend-proxy", None, start_ns, total_ns)]
    spans, logs = [], []
    root_end = start_ns + total_ns
    while queue and len(spans) < want:
        svc, parent, s_ns, d_ns = queue.pop(0)
        kind, ops, downstream = SERVICES[svc]
        span_id = _hex(8, rnd)
        op = root_op if svc == "frontend" else rnd.choice(ops)
        is_err = error_trace and (not downstream or rnd.random() < 0.3)
        e_ns = s_ns + d_ns
        pod = rnd.randint(0, 999)
        http_code = 500 if is_err else 200
        attrs = {"rpc.system": "grpc", "rpc.service": op.split("/")[0], "rpc.method": op.split("/")[-1],
                 "net.peer.name": svc, "app.product.id": rnd.choice(PRODUCTS),
                 "app.user.currency": rnd.choice(CURRENCIES)} if "/" in op and op.startswith("oteldemo") else \
                {"http.request.method": op.split(" ")[0] if " " in op else "GET", "url.path": op.split(" ")[-1],
                 "http.response.status_code": http_code, "server.address": svc,
                 "user_agent.original": "python-requests/2.31.0"}
        events = []
        if is_err:
            events.append({"name": "exception", "time": _iso(e_ns - 1000), "droppedAttributesCount": 0,
                           "attributes": {"exception.type": "RpcError",
                                          "exception.message": f"{op} failed: Unavailable"}})
        spans.append({
            "traceId": trace_id, "spanId": span_id, "parentSpanId": parent or "", "traceState": "",
            "name": op, "kind": kind, "serviceName": svc,
            "traceGroup": root_op if parent else op,
            "traceGroupFields": {"endTime": _iso(root_end), "durationInNanos": total_ns,
                                 "statusCode": 2 if error_trace else 0},
            "startTime": _iso(s_ns), "endTime": _iso(e_ns), "@timestamp": _iso(s_ns), "time": _iso(s_ns),
            "durationInNanos": d_ns,
            "status": {"code": 2 if is_err else 0, "message": "Unavailable" if is_err else ""},
            "attributes": attrs, "droppedAttributesCount": 0,
            "events": events, "droppedEventsCount": 0, "links": [], "droppedLinksCount": 0,
            "resource": _resource(svc, pod), "instrumentationScope": _scope(svc),
        })
        for _ in range(rnd.choice((0, 1, 1, 1, 2))):
            i = rnd.choices(range(len(LOG_TEMPLATES)), LOG_WEIGHTS)[0]
            sev_num, sev_txt, tmpl = LOG_TEMPLATES[i]
            if is_err and sev_num < 17 and rnd.random() < 0.5:
                sev_num, sev_txt, tmpl = LOG_TEMPLATES[rnd.choice((9, 10, 11))]
            t_ns = s_ns + rnd.randint(0, max(d_ns, 1))
            body = tmpl.format(op=op, user=f"u-{rnd.randint(1, 500000)}", ms=d_ns // 1_000_000, code=http_code,
                               product=rnd.choice(PRODUCTS), cart=_hex(6, rnd), qty=rnd.randint(1, 9),
                               order=_hex(8, rnd), amount=f"{rnd.uniform(5, 900):.2f}",
                               cur=rnd.choice(CURRENCIES), hit=rnd.random() < 0.8, ttl=rnd.randint(60, 3600),
                               peer=rnd.choice(downstream or [svc]))
            logs.append({
                "@timestamp": _iso(t_ns), "time": _iso(t_ns), "observedTime": _iso(t_ns + rnd.randint(1, 5) * 10**6),
                "traceId": trace_id, "spanId": span_id, "flags": 1,
                "severity": {"text": sev_txt, "number": sev_num}, "body": body, "droppedAttributesCount": 0,
                "attributes": {"log.iostream": "stdout", "code.function": op.split("/")[-1],
                               "app.order.id": _hex(8, rnd) if svc == "checkout" else "",
                               "thread.id": rnd.randint(1, 64)},
                "resource": _resource(svc, pod), "instrumentationScope": _scope(svc),
            })
        # Children split the parent's duration.
        kids = rnd.sample(downstream, min(len(downstream), rnd.randint(1, 3))) if downstream else []
        for k, child in enumerate(kids):
            c_d = max(d_ns // (len(kids) + 1), 100_000)
            queue.append((child, span_id, s_ns + (k + 1) * c_d // 2, c_d))
    return spans, logs


def service_map_docs():
    docs = []
    for svc, (kind, ops, downstream) in SERVICES.items():
        for d in downstream:
            for op in ops:
                docs.append({"timestamp": int(time.time() * 1000),
                             "nodeConnectionHash": f"{svc}->{d}", "operationConnectionHash": f"{svc}:{op}->{d}",
                             "sourceNode": {"type": "service", "keyAttributes": {"name": svc,
                                                                                 "environment": "otel-demo"}},
                             "targetNode": {"type": "service", "keyAttributes": {"name": d,
                                                                                 "environment": "otel-demo"}},
                             "sourceOperation": {"name": op, "attributes": {}},
                             "targetOperation": {"name": SERVICES[d][1][0], "attributes": {}}})
    return docs


# ---------------------------------------------------------------- bulk writer

class Bulker:
    def __init__(self, endpoint, auth):
        u = urllib.parse.urlparse(endpoint)
        self.host, self.port, self.auth = u.hostname, u.port or 443, auth
        self.conn = None

    def _post(self, path, body):
        for attempt in range(6):
            try:
                if self.conn is None:
                    self.conn = http.client.HTTPSConnection(self.host, self.port, context=CTX, timeout=120)
                self.conn.request("POST", path, body=body, headers={
                    "Authorization": self.auth, "Content-Type": "application/x-ndjson"})
                resp = self.conn.getresponse()
                data = resp.read()
                if resp.status == 429 or resp.status >= 500:
                    time.sleep(min(2 ** attempt, 30) * (0.5 + random.random()))
                    continue
                return resp.status, data
            except (OSError, http.client.HTTPException):
                self.conn = None
                time.sleep(min(2 ** attempt, 30))
        return 0, b""

    def send(self, alias, docs):
        """Bulk-index docs (list of JSON strings); retry only 429-rejected items. Returns (ok, failed)."""
        pending, failed = docs, 0
        for attempt in range(6):
            body = "".join('{"index":{}}\n' + d + "\n" for d in pending).encode()
            st, data = self._post(f"/{alias}/_bulk", body)
            if st != 200:
                if attempt == 5:
                    return len(docs) - len(pending), failed + len(pending)
                continue
            if b'"errors":false' in data[:200]:
                return len(docs) - failed, failed
            items = json.loads(data)["items"]
            retry = []
            for d, it in zip(pending, items):
                s = it["index"].get("status", 500)
                if s == 429:
                    retry.append(d)
                elif s >= 300:
                    failed += 1
            if not retry:
                return len(docs) - failed, failed
            pending = retry
            time.sleep(min(2 ** attempt, 30) * (0.5 + random.random()))
        return len(docs) - failed - len(pending), failed + len(pending)


def worker(idx, a, stop, ok_ctr, fail_ctr):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    rnd = random.Random(os.getpid() ^ int(time.time() * 1e6))
    bulker = Bulker(a.endpoint, _auth(a.username, a.password))
    spans_buf, logs_buf = [], []
    dumps = json.JSONEncoder(separators=(",", ":"), ensure_ascii=False).encode
    while not stop.is_set():
        now_ns = time.time_ns()
        while len(spans_buf) < a.bulk_size and len(logs_buf) < a.bulk_size:
            s, l = gen_trace(rnd, now_ns)
            spans_buf.extend(dumps(x) for x in s)
            logs_buf.extend(dumps(x) for x in l)
        for alias, buf in ((SPAN_ALIAS, spans_buf), (LOGS_ALIAS, logs_buf)):
            if len(buf) >= a.bulk_size:
                ok, bad = bulker.send(alias, buf)
                with ok_ctr.get_lock():
                    ok_ctr.value += ok
                with fail_ctr.get_lock():
                    fail_ctr.value += bad
                buf.clear()


def cluster_state(endpoint, auth):
    """(primary bytes across logs+spans, docs, max data-node disk %)"""
    st, rows = req(endpoint, auth, "GET",
                   f"/_cat/indices/{LOGS_ALIAS}-*,{SPAN_ALIAS}-*?format=json&bytes=b&h=index,docs.count,pri.store.size")
    pri = docs = 0
    if st == 200:
        for r in rows:
            pri += int(r.get("pri.store.size") or 0)
            docs += int(r.get("docs.count") or 0)
    st, alloc = req(endpoint, auth, "GET", "/_cat/allocation?format=json&h=node,disk.percent")
    disk = max((int(r["disk.percent"]) for r in alloc if r.get("disk.percent")), default=-1) if st == 200 else -1
    return pri, docs, disk


def cmd_run(a):
    auth = _auth(a.username, a.password)
    print(f"=== otel_dp_ingest: {a.procs} procs, bulk {a.bulk_size} -> {LOGS_ALIAS} + {SPAN_ALIAS} | "
          f"target {a.target_bytes / 2**40:.2f} TiB primary | stop at disk {DISK_STOP_PERCENT}% ===", flush=True)
    st, _ = req(a.endpoint, auth, "GET", f"/_alias/{LOGS_ALIAS},{SPAN_ALIAS}")
    if st != 200:
        sys.exit(f"aliases missing (HTTP {st}) — run `setup` first")
    smap = "".join('{"index":{}}\n' + json.dumps(d) + "\n" for d in service_map_docs()).encode()
    Bulker(a.endpoint, auth)._post(f"/{SERVICE_MAP_INDEX}/_bulk", smap)

    stop = mp.Event()
    ok_ctr, fail_ctr = mp.Value("q", 0), mp.Value("q", 0)
    procs = [mp.Process(target=worker, args=(i, a, stop, ok_ctr, fail_ctr), daemon=True) for i in range(a.procs)]
    for p in procs:
        p.start()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    start = last_t = time.time()
    last_ok, reason = 0, "stopped"
    try:
        while not stop.is_set():
            stop.wait(a.report_interval)
            now = time.time()
            ok, bad = ok_ctr.value, fail_ctr.value
            pri, docs, disk = cluster_state(a.endpoint, auth)
            alive = sum(p.is_alive() for p in procs)
            print(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now))} [{int(now - start):>6}s] "
                  f"{(ok - last_ok) / (now - last_t):>9,.0f} docs/s | client ok {ok:>14,} fail {bad:>9,} | "
                  f"cluster docs {docs:>14,} pri {pri / 2**40:7.3f} TiB | max disk {disk}% | procs {alive}",
                  flush=True)
            last_ok, last_t = ok, now
            if a.target_bytes and pri >= a.target_bytes:
                reason = f"target reached ({pri / 2**40:.2f} TiB)"
                break
            if disk >= DISK_STOP_PERCENT:
                reason = f"disk guard tripped ({disk}%)"
                break
            if a.duration and now - start >= a.duration:
                reason = "duration cap"
                break
    finally:
        stop.set()
        for p in procs:
            p.join(timeout=60)
        print(f"=== done: {reason} | client ok={ok_ctr.value:,} fail={fail_ctr.value:,} | "
              f"elapsed {int(time.time() - start)}s ===", flush=True)


def _corpus_part(idx, out_dir, n_spans, seed):
    rnd = random.Random(seed + idx)
    dumps = json.JSONEncoder(separators=(",", ":"), ensure_ascii=False).encode
    ns = nl = 0
    # Spread corpus timestamps over the 24h before generation so time-range queries have a window.
    end_ns = time.time_ns()
    with open(f"{out_dir}/spans.part{idx:03d}", "w") as fs, open(f"{out_dir}/logs.part{idx:03d}", "w") as fl:
        while ns < n_spans:
            spans, logs = gen_trace(rnd, end_ns - rnd.randint(0, 86_400) * 1_000_000_000)
            for s in spans:
                fs.write(dumps(s) + "\n")
            for l in logs:
                fl.write(dumps(l) + "\n")
            ns += len(spans)
            nl += len(logs)
    return ns, nl


def cmd_corpus(a):
    """Write OSB corpus files (one doc per line, no action lines) for the otel_dp workload."""
    os.makedirs(a.out_dir, exist_ok=True)
    per = -(-a.spans // a.procs)
    t0 = time.time()
    with mp.Pool(a.procs) as pool:
        counts = pool.starmap(_corpus_part, [(i, a.out_dir, per, a.seed) for i in range(a.procs)])
    for kind in ("spans", "logs"):
        # Concatenate the parts and write OSB's file offset table ("<line>;<byte offset>" every
        # 50000 lines, same as osbenchmark.utils.io.prepare_file_offset_table) on the way, so OSB
        # doesn't spend minutes re-scanning a ~150 GB file at the start of the next chunk.
        data = f"{a.out_dir}/{kind}.json"
        lines = pos = 0
        nxt = 50000
        with open(data, "wb") as out, open(data + ".offset.tmp", "w") as off:
            for i in range(a.procs):
                p = f"{a.out_dir}/{kind}.part{i:03d}"
                with open(p, "rb") as f:
                    while chunk := f.read(64 << 20):
                        out.write(chunk)
                        n = chunk.count(b"\n")
                        end = lines + n
                        idx, seen = -1, lines
                        while nxt <= end:  # walk only as far as each 50000-line boundary in this chunk
                            for _ in range(nxt - seen):
                                idx = chunk.index(b"\n", idx + 1)
                            seen = nxt
                            off.write(f"{nxt};{pos + idx + 1}\n")
                            nxt += 50000
                        lines = end
                        pos += len(chunk)
                os.remove(p)
        os.replace(data + ".offset.tmp", data + ".offset")  # mtime >= data file -> OSB treats it as valid
    with open(f"{a.out_dir}/service-map.json", "w") as f:
        smap = service_map_docs()
        f.writelines(json.dumps(d) + "\n" for d in smap)
    ns, nl = sum(c[0] for c in counts), sum(c[1] for c in counts)
    meta = {"spans_document_count": ns, "logs_document_count": nl, "service_map_document_count": len(smap),
            "spans_bytes": os.path.getsize(f"{a.out_dir}/spans.json"),
            "logs_bytes": os.path.getsize(f"{a.out_dir}/logs.json"), "seed": a.seed,
            "elapsed_s": int(time.time() - t0)}
    json.dump(meta, open(f"{a.out_dir}/corpus-meta.json", "w"), indent=2)
    print(json.dumps(meta, indent=2))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("corpus", help="write OSB corpus files for the otel_dp workload")
    c.add_argument("--out-dir", required=True)
    c.add_argument("--spans", type=int, default=100_000_000, help="approx span docs (logs ≈ same)")
    c.add_argument("--procs", type=int, default=os.cpu_count())
    c.add_argument("--seed", type=int, default=20261007)
    for name in ("setup", "run"):
        p = sub.add_parser(name)
        p.add_argument("--endpoint", required=True)
        p.add_argument("--username", default=os.environ.get("OSB_USER", "admin"))
        p.add_argument("--password", default=os.environ.get("OSB_PASSWORD"), required="OSB_PASSWORD" not in os.environ)
    s = sub.choices["setup"]
    s.add_argument("--shards", type=int, default=10)
    s.add_argument("--replicas", type=int, default=1)
    s.add_argument("--rollover-shard-size", default="50gb")
    r = sub.choices["run"]
    r.add_argument("--procs", type=int, default=os.cpu_count())
    r.add_argument("--bulk-size", type=int, default=5000)
    r.add_argument("--target-bytes", type=int, default=10 * 2**40)
    r.add_argument("--duration", type=int, default=0, help="hard cap seconds (0 = none)")
    r.add_argument("--report-interval", type=int, default=30)
    a = ap.parse_args()
    if a.cmd == "corpus":
        return cmd_corpus(a)
    a.endpoint = a.endpoint.rstrip("/")
    if not a.endpoint.startswith("http"):
        a.endpoint = "https://" + a.endpoint
    {"setup": cmd_setup, "run": cmd_run}[a.cmd](a)


if __name__ == "__main__":
    main()
