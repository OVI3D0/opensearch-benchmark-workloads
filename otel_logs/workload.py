import json
import os
import random
import re


class RandomPPLParamSource:
    """Picks a random PPL query from operations/default.json on each call.

    Used by the otel-logs-random-mix test_procedure to drive sustained random
    query load at a configurable TPS, instead of OSB's default per-query mode.
    """

    def __init__(self, workload, params, **kwargs):
        index_name = params.get("index_name", "otel_logs")
        ops_path = os.path.join(os.path.dirname(__file__), "operations", "default.json")
        with open(ops_path) as f:
            raw = f.read()

        # operations/default.json is a fragment (concatenated by benchmark.collect)
        # and contains Jinja blocks. Render the index_name we care about, then null
        # out any remaining {{...}} so json.loads succeeds. The non-PPL ops get
        # filtered out below regardless.
        raw = re.sub(r"\{\{\s*index_name[^}]*\}\}", index_name, raw)
        raw = re.sub(r"\{\{[^}]*\}\}", "null", raw)
        ops = json.loads("[" + raw + "]")

        self._queries = [
            {"method": op["method"], "path": op["path"], "body": op["body"]}
            for op in ops
            if op.get("operation-type") == "raw-request"
            and op.get("path") == "/_plugins/_ppl"
            and re.match(r"^q\d{2}-", op.get("name", ""))
        ]
        if not self._queries:
            raise ValueError(f"no PPL queries found in {ops_path}")

    def partition(self, partition_index, total_partitions):
        return self

    def params(self):
        q = random.choice(self._queries)
        return {"method": q["method"], "path": q["path"], "body": q["body"]}


def register(registry):
    registry.register_param_source("random-ppl-mix", RandomPPLParamSource)
