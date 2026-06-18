import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from common.random_query_param_source import build_param_source


def register(registry):
    registry.register_param_source(
        "random-ppl-mix",
        build_param_source(workload_dir=__file__, default_index="textbench-*"),
    )
