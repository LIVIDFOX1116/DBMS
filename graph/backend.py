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

GPU is opt-in, not automatic: even with RAPIDS importable, `gpu_available()`
returns False unless TXGRAPH_USE_GPU_GRAPH=1 is also set. This matters for a
wallet clustering tool specifically because cuGraph Louvain and NetworkX
Louvain are not guaranteed to produce the same partitions on the same data
(different algorithm implementations -- parallel-GPU vs sequential-CPU --
and NetworkX's louvain_communities takes a fixed seed for reproducibility
while cuGraph's louvain() exposes no seed at all) -- a RAPIDS-capable
environment showing up by accident (e.g. a stray WSL2/conda activation)
must not silently change which clusters a user sees. TXGRAPH_FORCE_CPU
still overrides everything, opt-in flag included -- it's the "definitely
never GPU, no matter what" escape hatch, kept exactly as before.

Force the CPU path even when RAPIDS is installed and opted in with:
    GraphBackend(force_cpu=True)
or:
    TXGRAPH_FORCE_CPU=1 python ...

Opt in to the GPU path (also requires RAPIDS actually importable) with:
    TXGRAPH_USE_GPU_GRAPH=1 python ...
"""
import os
import time
import logging

log = logging.getLogger("graph.backend")


def gpu_available() -> bool:
    if os.getenv("TXGRAPH_FORCE_CPU", "").lower() in ("1", "true", "yes"):
        return False
    if os.getenv("TXGRAPH_USE_GPU_GRAPH", "").lower() not in ("1", "true", "yes"):
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

        if self.use_gpu:
            import cudf
            import cugraph
            self._cudf = cudf
            self._cugraph = cugraph
            self.library_version = cugraph.__version__
        else:
            import pandas
            import networkx
            self._pandas = pandas
            self._networkx = networkx
            self.library_version = networkx.__version__
        log.info("GraphBackend using: %s (library version %s)", self.backend_name, self.library_version)

    # ------------------------------------------------------------ load

    def load_edgelist(self, edges_df, src="src_wallet_id", dst="dst_wallet_id", weight="edge_weight"):
        """
        `edges_df` is a plain pandas.DataFrame (as produced by
        export_snapshot.py from Postgres / Parquet). Returns a backend-native
        graph object opaque to the caller -- pass it back into pagerank(),
        louvain(), bfs() below.
        """
        if self.use_gpu:
            gdf = self._cudf.from_pandas(edges_df)
            G = self._cugraph.Graph(directed=True)
            # store_transposed=True avoids pagerank() silently making its own
            # transposed copy of the graph at call time -- worth avoiding on
            # the GTX 1650's 4GB VRAM budget, not just the warning it throws.
            G.from_cudf_edgelist(gdf, source=src, destination=dst, edge_attr=weight,
                                  renumber=True, store_transposed=True)
            # cugraph's Graph.to_undirected() errors on weighted graphs with
            # reciprocal edges (src size != weights size), so louvain() below
            # rebuilds a fresh undirected graph from the original edgelist
            # instead -- stash the column names needed to do that.
            G._txgraph_cols = (src, dst, weight)
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

    def pagerank_excluding_signed_edges(self, edges_df, exclude_wallet_id,
                                         src="src_wallet_id", dst="dst_wallet_id", weight="edge_weight"):
        """Re-runs PageRank with every edge SIGNED by `exclude_wallet_id`
        (every row where it's the source) masked out first -- the audit
        engine's (graph/audit.py) frameable/robust check for
        pagerank_threshold: does wallet W's rank still clear the flag's
        threshold once W's own outgoing activity is removed from the
        graph? On GPU, the mask is a cuDF boolean filter applied before
        rebuilding the graph, so the masking itself runs GPU-side, not
        just the pagerank call after it; on CPU, the same filter via
        pandas. Returns the same pandas.DataFrame[wallet_id, score] shape
        pagerank() does."""
        if self.use_gpu:
            gdf = self._cudf.from_pandas(edges_df)
            masked = gdf[gdf[src] != exclude_wallet_id]
            G = self._cugraph.Graph(directed=True)
            G.from_cudf_edgelist(masked, source=src, destination=dst, edge_attr=weight,
                                  renumber=True, store_transposed=True)
        else:
            masked_df = edges_df[edges_df[src] != exclude_wallet_id]
            G = self._networkx.from_pandas_edgelist(
                masked_df, source=src, target=dst, edge_attr=weight, create_using=self._networkx.DiGraph(),
            )
        return self.pagerank(G)

    def louvain(self, G):
        """Returns (pandas.DataFrame[wallet_id, community_label], modularity_score)."""
        t0 = time.time()
        if self.use_gpu:
            src, dst, weight = G._txgraph_cols
            UG = self._cugraph.Graph(directed=False)
            UG.from_cudf_edgelist(G.view_edge_list(), source=src, destination=dst, edge_attr=weight, renumber=True)
            result, modularity = self._cugraph.louvain(UG)
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
            # cugraph.bfs returns a row for every vertex, using int64-max as a
            # sentinel distance for unreached ones -- NetworkX's path below
            # only returns reached vertices, so drop the sentinel rows to keep
            # both backends' output identical (and not write garbage int64s
            # into vertex_metric.metric_value for unreached wallets).
            result = result[result["distance"] != (2 ** 63) - 1]
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
