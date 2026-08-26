"""
GPU/CPU graph backend abstraction.

On the RTX 4070 / GTX 1650 laptops (with RAPIDS installed), this transparently
uses cudf + cugraph. In this sandbox -- or anywhere RAPIDS isn't installed --
it falls back to pandas + NetworkX automatically, so the pipeline's *logic*
(not its speed) is testable anywhere, per the project bible's Section 7.3
philosophy applied to the backend half too: don't let a GPU-only dependency
block development of everything downstream of it.

Every public method returns a plain pandas.DataFrame regardless of which
backend actually ran the algorithm -- callers (run_analysis.py, the API layer)
never need an if/else on which backend is active. The GPU, when used, only
ever holds a transient copy of the graph; results are always normalized back
to pandas before this module returns them, mirroring the "GPU is transient,
Postgres is durable" rule from the ER diagram notes.

Force the CPU path even when RAPIDS is installed with:
    GraphBackend(force_cpu=True)
or:
    TXGRAPH_FORCE_CPU=1 python ...
"""
import os
import time
import logging

log = logging.getLogger("graph.backend")


def gpu_available() -> bool:
    if os.getenv("TXGRAPH_FORCE_CPU", "").lower() in ("1", "true", "yes"):
        return False
    try:
        import cudf  # noqa: F401
        import cugraph  # noqa: F401
        return True
    except ImportError:
        return False


class GraphBackend:
    def __init__(self, force_cpu: bool = False):
        self.use_gpu = gpu_available() and not force_cpu
        self.backend_name = "cudf+cugraph (GPU)" if self.use_gpu else "pandas+networkx (CPU)"
        log.info("GraphBackend using: %s", self.backend_name)

        if self.use_gpu:
            import cudf
            import cugraph
            self._cudf = cudf
            self._cugraph = cugraph
        else:
            import pandas
            import networkx
            self._pandas = pandas
            self._networkx = networkx

    # ------------------------------------------------------------ load

    def load_edgelist(self, edges_df, src="src_wallet_id", dst="dst_wallet_id", weight="edge_weight"):
        """
        `edges_df` is a plain pandas.DataFrame (as produced by
        export_snapshot.py from Postgres / Parquet). Returns a backend-native
        graph object opaque to the caller -- pass it back into pagerank(),
        louvain(), bfs() below.
        """
        if self.use_gpu:
            gdf = self._cudf.DataFrame.from_pandas(edges_df)
            G = self._cugraph.Graph(directed=True)
            G.from_cudf_edgelist(gdf, source=src, destination=dst, edge_attr=weight, renumber=True)
            return G
        else:
            G = self._networkx.from_pandas_edgelist(
                edges_df, source=src, target=dst, edge_attr=weight,
                create_using=self._networkx.DiGraph(),
            )
            return G

    # ------------------------------------------------------------- algorithms

    def pagerank(self, G):
        """Returns a pandas.DataFrame[wallet_id, score], sorted descending."""
        t0 = time.time()
        if self.use_gpu:
            result = self._cugraph.pagerank(G)
            df = result.to_pandas().rename(columns={"vertex": "wallet_id", "pagerank": "score"})
        else:
            scores = self._networkx.pagerank(G, weight="edge_weight")
            df = self._pandas.DataFrame(scores.items(), columns=["wallet_id", "score"])
        df = df.sort_values("score", ascending=False).reset_index(drop=True)
        log.info("pagerank (%s): %d vertices in %.2fs", self.backend_name, len(df), time.time() - t0)
        return df

    def louvain(self, G):
        """Returns (pandas.DataFrame[wallet_id, community_label], modularity_score)."""
        t0 = time.time()
        if self.use_gpu:
            result, modularity = self._cugraph.louvain(G)
            df = result.to_pandas().rename(columns={"vertex": "wallet_id", "partition": "community_label"})
        else:
            undirected = G.to_undirected()
            communities = self._networkx.algorithms.community.louvain_communities(
                undirected, weight="edge_weight", seed=42,
            )
            modularity = self._networkx.algorithms.community.modularity(
                undirected, communities, weight="edge_weight",
            )
            rows = [
                {"wallet_id": node, "community_label": i}
                for i, community in enumerate(communities)
                for node in community
            ]
            df = self._pandas.DataFrame(rows)
        log.info("louvain (%s): %d communities, modularity=%.4f in %.2fs",
                  self.backend_name, df["community_label"].nunique(), modularity, time.time() - t0)
        return df, float(modularity)

    def bfs(self, G, start_vertex):
        """Returns pandas.DataFrame[wallet_id, distance, predecessor]."""
        t0 = time.time()
        if self.use_gpu:
            result = self._cugraph.bfs(G, start_vertex)
            df = result.to_pandas().rename(columns={"vertex": "wallet_id"})
        else:
            lengths = self._networkx.single_source_shortest_path_length(G, start_vertex)
            preds = {}
            for target, path in self._networkx.single_source_shortest_path(G, start_vertex).items():
                preds[target] = path[-2] if len(path) > 1 else -1
            df = self._pandas.DataFrame(
                [{"wallet_id": v, "distance": d, "predecessor": preds.get(v, -1)} for v, d in lengths.items()]
            )
        log.info("bfs (%s): reached %d vertices from %s in %.2fs",
                  self.backend_name, len(df), start_vertex, time.time() - t0)
        return df
