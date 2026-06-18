"""Generic random-query parameter source for OSB workloads.

A workload's `workload.py` registers this once, and a test_procedure entry can
fan-out a single 'random query' operation that picks uniformly at random from
the workload's query operations on every iteration.

The param source filters operations by:
  - operation-type == 'raw-request'
  - operation 'path' matches one of the configured filter_paths

That naturally excludes admin / setup / ingest ops. To register:

    from common.random_query_param_source import build_param_source

    def register(registry):
        registry.register_param_source(
            "random-query-mix",
            build_param_source(workload_dir=__file__, default_index="big5"),
        )

Then in test_procedures/<name>.json:

    {
      "operation": {
        "name": "random-query",
        "operation-type": "raw-request",
        "param-source": "random-query-mix",
        "operations_path": "operations/ppl.json",   # optional, defaults to all operations/*.json
        "filter_paths": ["/_plugins/_ppl"],          # optional, defaults to ['/_plugins/_ppl', '/_search']
        "index_name": "{{ index_name | default('big5') }}"
      },
      "warmup-time-period": 60,
      "time-period": 600,
      "target-throughput": 1000,
      "clients": 100
    }

OSB will instantiate the param source once (with the operation's params dict)
and call params() per iteration; each call returns a {method, path, body} dict
that the raw-request runner sends as-is.
"""

import glob
import json
import os
import random
import re

DEFAULT_FILTER_PATHS = ("/_plugins/_ppl", "/_search")


def _load_ops(workload_dir: str, ops_glob: str, index_name: str):
    """Load JSON op fragments under workload_dir/<ops_glob>, render the index_name
    Jinja, return parsed list of operation dicts."""
    pattern = os.path.join(workload_dir, ops_glob)
    paths = sorted(glob.glob(pattern))
    if not paths:
        raise ValueError(f"no operation files matched {pattern}")
    fragments = []
    for p in paths:
        with open(p) as f:
            fragments.append(f.read())
    raw = ",\n".join(fragments)

    try:
        from jinja2 import Environment
        # Render the index_name var and any benign Jinja in the bodies. We pass
        # distribution_version because some workloads gate ops on it.
        raw = Environment().from_string(raw).render(
            index_name=index_name, distribution_version="3.5.0"
        )
    except ImportError:
        # Fallback: minimal substitution.
        raw = re.sub(r"\{\{\s*index_name[^}]*\}\}", index_name, raw)
        raw = re.sub(r"\{%.*?%\}", "", raw, flags=re.DOTALL)
        raw = re.sub(r"\{\{[^}]*\}\}", "null", raw)

    return json.loads("[" + raw + "]")


class _RandomQueryParamSource:
    def __init__(self, workload, params, *, _workload_dir, _default_index, **kwargs):
        index_name = params.get("index_name", _default_index)
        ops_glob = params.get("operations_path", "operations/*.json")
        filter_paths = tuple(params.get("filter_paths", DEFAULT_FILTER_PATHS))

        ops = _load_ops(_workload_dir, ops_glob, index_name)
        self._queries = [
            {"method": op["method"], "path": op["path"], "body": op["body"]}
            for op in ops
            if op.get("operation-type") == "raw-request"
            and op.get("path") in filter_paths
        ]
        if not self._queries:
            raise ValueError(
                f"no query operations matched in {ops_glob} "
                f"(filter_paths={filter_paths}); check operations file + filter"
            )

    def partition(self, partition_index, total_partitions):
        return self

    def params(self):
        q = random.choice(self._queries)
        return {"method": q["method"], "path": q["path"], "body": q["body"]}


def build_param_source(*, workload_dir: str, default_index: str):
    """Return a class suitable for registry.register_param_source(...).

    workload_dir: pass __file__ from the workload.py; we resolve the dir.
    default_index: index_name to use when the test_procedure doesn't override.
    """
    wdir = os.path.dirname(os.path.abspath(workload_dir))

    class RandomQueryParamSource(_RandomQueryParamSource):
        def __init__(self, workload, params, **kwargs):
            super().__init__(
                workload, params,
                _workload_dir=wdir, _default_index=default_index, **kwargs
            )

    return RandomQueryParamSource
