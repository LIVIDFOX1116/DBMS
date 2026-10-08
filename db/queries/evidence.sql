-- FrameCheck Phase 4: evidence chain for a flagged wallet W.
--
-- Params (named, psycopg2 dict-style): %(snapshot_id)s, %(wallet_id)s (the
-- flagged wallet, W), %(seed_wallet_id)s, %(max_hops)s (cycle/runaway guard).
--
-- Two UNIONed parts:
--
--   1. hop=0: every direct transaction touching W in this snapshot.
--      role = 'signed' if W is the signer (t.from_wallet_id = W);
--      role = 'unsolicited' if W is the recipient and did NOT sign
--      (t.to_wallet_id = W, from_wallet_id != W). Role is computed from
--      transaction.from_wallet_id ONLY -- never token_transfer.from_wallet_id
--      (see CLAUDE.md's definitions: that column is attacker-spoofable via
--      ERC-20 Transfer event logs). W's own contract-creation transactions
--      (to_wallet_id IS NULL) still come through correctly on the 'signed'
--      side: the 'signed' half of the CASE only reads from_wallet_id, and
--      NULL to_wallet_id can never equal W, so it can't be mis-routed into
--      the 'unsolicited' half either -- that's the explicit NULL
--      to_wallet_id handling this file needs.
--
--   2. hop>=1: recursively, for every 'unsolicited' wallet discovered so
--      far, follow its OWN single earliest inbound transaction in this
--      snapshot (the edge that first brought it into the crawl, i.e. its
--      own hop=0-equivalent) one more hop back, continuing until the seed
--      wallet is reached, the hop cap is hit, or the predecessor was
--      already visited in this chain (a `visited` array prevents
--      re-walking a cycle). Every hop>=1 row is 'unsolicited' by
--      construction: it is, from that predecessor's own perspective, a
--      transfer it received, not one it signed.
--
-- This asymmetric design (hop=0 sees both of W's directions; hop>=1 only
-- follows the inbound/unsolicited side) is deliberate, and feeds BOTH
-- halves of the (corrected 2026-10-06) audit algorithm in graph/audit.py:
--   - frameable/robust: remove every hop=0 role='signed' row (W passive)
--     and re-run the detector -- these are the rows the audit strips to
--     build that graph.
--   - framing cost (frameable flags only): starting from W-passive graph
--     with its hop=0 role='unsolicited' rows ALSO removed, add those same
--     real rows back cheapest-first (value_wei + gas) until the flag
--     reappears, then prune. The attacker can only add transfers into the
--     target (never rewrite an ancestor wallet's own history), so only
--     hop=0 rows are ever candidates to add back -- hop>=1 rows are
--     retained purely as provenance/narrative (and for the bfs_proximity
--     exact-cost audit, which needs to know how close an upstream wallet
--     already is to the seed), never as addable evidence themselves.
--
-- Final result is block-ordered (chronological), per the Phase 4 spec --
-- NOT grouped by hop.

WITH RECURSIVE evidence(from_wallet_id, tx_hash, block_number, tx_index, role, hop, visited) AS (
    -- hop 0, native: transactions whose snapshot edge touches W. Driven from
    -- snapshot_edge (idx_se_src / idx_se_dst) into snapshot_edge_tx's
    -- (snapshot_edge_id, tx_hash) primary key; there is no tx_hash index.
    SELECT
        t.from_wallet_id,
        t.tx_hash, t.block_number, t.tx_index,
        CASE WHEN t.from_wallet_id = %(wallet_id)s THEN 'signed' ELSE 'unsolicited' END AS role,
        0 AS hop,
        ARRAY[%(wallet_id)s::bigint] AS visited
    FROM snapshot_edge se
    JOIN snapshot_edge_tx st ON st.snapshot_edge_id = se.edge_id
    JOIN transaction t ON t.tx_hash = st.tx_hash
    WHERE se.snapshot_id = %(snapshot_id)s
      AND (se.src_wallet_id = %(wallet_id)s OR se.dst_wallet_id = %(wallet_id)s)

    UNION ALL

    -- hop 0, token: W received an ERC-20 transfer. transaction.to_wallet_id
    -- is the token CONTRACT there, so W is only visible via
    -- token_transfer.to_wallet_id. The role/signer still comes from
    -- transaction.from_wallet_id; a token transfer W signed is already
    -- covered above as 'signed'.
    SELECT
        t.from_wallet_id,
        t.tx_hash, t.block_number, t.tx_index,
        'unsolicited' AS role,
        0 AS hop,
        ARRAY[%(wallet_id)s::bigint] AS visited
    FROM token_transfer tt
    JOIN transaction t ON t.tx_hash = tt.tx_hash
    JOIN snapshot_edge se ON se.snapshot_id = %(snapshot_id)s
                         AND se.src_wallet_id = t.from_wallet_id
                         AND se.dst_wallet_id = t.to_wallet_id
    JOIN snapshot_edge_tx st ON st.snapshot_edge_id = se.edge_id AND st.tx_hash = t.tx_hash
    WHERE tt.to_wallet_id = %(wallet_id)s
      AND t.from_wallet_id <> %(wallet_id)s

    UNION ALL

    -- Each worktable generation is deduplicated by from_wallet_id: a wallet's
    -- earliest inbound transaction does not depend on which of its many
    -- transfers led here, so walking it once per distinct wallet is enough.
    SELECT
        t2.from_wallet_id,
        t2.tx_hash, t2.block_number, t2.tx_index,
        'unsolicited' AS role,
        e.hop + 1 AS hop,
        e.visited || e.from_wallet_id
    FROM (
        SELECT DISTINCT ON (ev.from_wallet_id) ev.from_wallet_id, ev.hop, ev.visited
        FROM evidence ev
        WHERE ev.role = 'unsolicited'
          AND ev.from_wallet_id IS NOT NULL
          AND ev.from_wallet_id != %(seed_wallet_id)s
          AND ev.hop < %(max_hops)s
          AND NOT (ev.from_wallet_id = ANY(ev.visited))
        ORDER BY ev.from_wallet_id
    ) e
    JOIN LATERAL (
        SELECT t.from_wallet_id, t.tx_hash, t.block_number, t.tx_index
        FROM snapshot_edge se
        JOIN snapshot_edge_tx st ON st.snapshot_edge_id = se.edge_id
        JOIN transaction t ON t.tx_hash = st.tx_hash
        WHERE se.snapshot_id = %(snapshot_id)s
          AND se.dst_wallet_id = e.from_wallet_id
        ORDER BY t.block_number, t.tx_index
        LIMIT 1
    ) t2 ON TRUE
)
SELECT DISTINCT tx_hash, hop, block_number, role
FROM evidence
ORDER BY block_number, hop;
