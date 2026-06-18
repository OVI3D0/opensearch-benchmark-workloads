import json
import os
import random
import re


class RandomPPLParamSource:
    """Picks a random PPL operation from operations/ppl.json on each call.

    Used by the redline test_procedure to drive sustained random query load
    at a high TPS target, with each client picking from the workload's full
    PPL query set. Mirrors otel_logs' RandomPPLParamSource.
    """

    def __init__(self, workload, params, **kwargs):
        index_name = params.get("index_name", "big5")
        ops_path = os.path.join(os.path.dirname(__file__), "operations", "ppl.json")
        with open(ops_path) as f:
            raw = f.read()

        # operations/ppl.json is a fragment (concatenated by benchmark.collect),
        # so wrap it in [] and resolve the only Jinja var we use in the bodies.
        # Render the OSB-time Jinja with the concrete index_name so query bodies
        # are valid JSON strings post-substitution.
        try:
            from jinja2 import Environment
            # distribution_version satisfies {% if %} blocks in some op files;
            # value isn't important since the conditional bodies all produce valid JSON.
            raw = Environment().from_string(raw).render(
                index_name=index_name, distribution_version="3.5.0"
            )
        except ImportError:
            # Fallback: strip Jinja minimally if jinja2 isn't importable here
            raw = re.sub(r"\{\{\s*index_name[^}]*\}\}", index_name, raw)
            raw = re.sub(r"\{%.*?%\}", "", raw, flags=re.DOTALL)
            raw = re.sub(r"\{\{[^}]*\}\}", "null", raw)
        ops = json.loads("[" + raw + "]")

        self._queries = [
            {"method": op["method"], "path": op["path"], "body": op["body"]}
            for op in ops
            if op.get("operation-type") == "raw-request"
            and op.get("path") == "/_plugins/_ppl"
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
