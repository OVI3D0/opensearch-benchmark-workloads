# SPDX-License-Identifier: Apache-2.0
#
# The OpenSearch Contributors require contributions made to
# this file be licensed under the Apache-2.0 license or a
# compatible open source license.

from .runners import register as register_runners
from osbenchmark.workload.params import (
    ParamSource,
    VectorSearchParamSource,
    VectorSearchPartitionParamSource,
)
import random
import numpy as np
import logging


def register(registry):
    register_runners(registry)
    # Register random-vector param-sources
    registry.register_param_source("random-vector-bulk-param-source", RandomBulkParamSource)
    registry.register_param_source("random-vector-search-param-source", RandomSearchParamSource)
    # Vespa-native vector search param-source — emits pre-translated YQL directly
    # so the Vespa runner skips DSL→YQL translation. Used by the vespa-search-only
    # test procedure.
    registry.register_param_source("vespa-vector-search-param-source", VespaVectorSearchParamSource)


class RandomBulkParamSource(ParamSource):
    def __init__(self, workload, params, **kwargs):
        super().__init__(workload, params, **kwargs)
        logging.getLogger(__name__).info("Workload: [%s], params: [%s]", workload, params)
        self._bulk_size = params.get("bulk-size", 100)
        self._index_name = params.get('index_name','target_index')
        self._field = params.get("field", "target_field")
        self._dims = params.get("dims", 768)
        self._partitions = params.get("partitions", 1000)

    def partition(self, partition_index, total_partitions):
        return self

    def params(self):
        bulk_data = []
        for _ in range(self._bulk_size):
            vec = np.random.rand(self._dims)
            partition_id = random.randint(0, self._partitions)
            metadata = {"_index": self._index_name}
            bulk_data.append({"create": metadata})
            bulk_data.append({"partition_id": partition_id, self._field: vec.tolist()})

        return {
            "body": bulk_data,
            "bulk-size": self._bulk_size,
            "action-metadata-present": True,
            "unit": "docs",
            "index": self._index_name,
            "type": "",
        }

class RandomSearchParamSource(ParamSource):
    def __init__(self, workload, params, **kwargs):
        super().__init__(workload, params, **kwargs)
        logging.getLogger(__name__).info("Workload: [%s], params: [%s]", workload, params)
        self._index_name = params.get('index_name', 'target_index')
        self._dims = params.get("dims", 768)
        self._cache = params.get("cache", False)
        self._top_k = params.get("k", 100)
        self._field = params.get("field", "target_field")
        self._query_body = params.get("body", {})

    def partition(self, partition_index, total_partitions):
        return self

    def params(self):
        query_vec = np.random.rand(self._dims).tolist()
        query = self.generate_knn_query(query_vec)
        query.update(self._query_body)
        return {"index": self._index_name, "cache": self._cache, "size": self._top_k, "body": query}

    def generate_knn_query(self, query_vector):
        return {
            "query": {
                "knn": {
                    self._field: {
                        "vector": query_vector,
                        "k": self._top_k
                    }
                }
            }
        }

class VespaVectorSearchPartitionParamSource(VectorSearchPartitionParamSource):
    """Partition param source that produces Vespa-native search bodies directly.

    Reuses the parent class's HDF5 vector reading, neighbor loading, recall
    tracking, etc. Only overrides body construction: instead of building
    OpenSearch KNN DSL (which the Vespa runner would then translate to YQL),
    we build the Vespa search body directly.

    Body shape produced:
        {
            "yql": "select documentid from <index> where {targetHits:K}nearestNeighbor(<field>, query_vector)",
            "ranking": "vector-similarity",
            "hits": K,
            "input.query(query_vector)": [<vector>]
        }

    The Vespa runner detects the top-level "yql" key and passes the body
    through unchanged, skipping convert_to_yql entirely. This removes DSL→YQL
    translation from the benchmark's critical path so measured throughput
    reflects engine performance rather than client translation overhead.

    Note: ef_search / hnsw.exploreAdditionalHits is still injected by the
    Vespa runner at query time via .replace() on the targetHits pattern, so
    you can still tune exploration depth via --client-options=hnsw_ef_search:N
    without rebuilding the body.
    """

    def _update_body_params(self, vector):
        body_params = self.query_params.get(self.PARAMS_NAME_BODY) or dict()

        index_name = self.query_params.get("index", "target_index")
        field_name = self.query_params.get("field", "target_field")

        if hasattr(vector, "tolist"):
            vector = vector.tolist()

        body_params["yql"] = (
            f"select documentid from {index_name} where "
            f"{{targetHits:{self.k}}}nearestNeighbor({field_name}, query_vector)"
        )
        body_params["ranking"] = "vector-similarity"
        body_params["hits"] = self.k
        body_params["input.query(query_vector)"] = vector

        self.query_params[self.PARAMS_NAME_BODY] = body_params


class VespaVectorSearchParamSource(VectorSearchParamSource):
    """Top-level param source wrapper for Vespa-native vector search.

    Delegates to VespaVectorSearchPartitionParamSource for the per-client
    partition. Structurally identical to VectorSearchParamSource except for
    the delegate class.
    """

    def __init__(self, workload, params, **kwargs):
        logging.getLogger(__name__).info(
            "Workload: [%s], params: [%s] (Vespa-native YQL)", workload, params
        )
        # Bypass VectorSearchParamSource's __init__ which hard-codes the
        # OS partition class, and call SearchParamSource.__init__ directly.
        from osbenchmark.workload.params import SearchParamSource
        SearchParamSource.__init__(self, workload, params, **kwargs)
        self.delegate_param_source = VespaVectorSearchPartitionParamSource(
            workload, params, self.query_params, **kwargs
        )
        self.corpora = self.delegate_param_source.corpora
