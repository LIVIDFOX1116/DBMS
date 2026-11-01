# Normalization

The relational schema is `db/schema.sql` plus `db/migrations/`. It is documented table by table in
[`DATA_DICTIONARY.md`](DATA_DICTIONARY.md), and drawn from [`schema_v2.dbml`](schema_v2.dbml) in the
[ER diagram](ER_Diagram_TransactionGraphAnalyzer.svg). This page records how the 25 tables were checked against first,
second and third normal form, including where they knowingly fall short.

## How each form was checked

- **1NF:** every column holds one value and every table has a primary key. All 25 tables have one.
- **2NF:** in a table with a composite key, no non-key column depends on only part of that key.
- **3NF:** no non-key column depends on another non-key column.

`jsonb` columns are treated as single opaque values, because no query or constraint reaches inside them for relational
data: `algorithm.default_params`, `analysis_run.parameters`, `flag_audit.minimal_tx_set`, `flag_history.before_image`.

## Tables in 3NF

| Group | Tables | Why |
|---|---|---|
| Single-column key, facts about that key only | `wallet`, `block`, `transaction`, `token`, `token_transfer`, `label`, `graph_snapshot`, `snapshot_edge`, `algorithm`, `analysis_run`, `vertex_metric`, `cluster`, `visualization`, `flag`, `flag_audit`, `flag_history`, `app_user`, `schema_migration` | No non-key column determines another. Alternate unique columns (`wallet.address`, `block.block_hash`, `label.label_name`, `app_user.username` and similar) are candidate keys, which 3NF allows |
| Composite key, columns describe the whole pair | `wallet_label`, `cluster_member`, `snapshot_edge_tx` | For example `confidence` and `tagged_by` describe one label on one wallet, and `membership_score` describes one wallet in one cluster |

These many-to-many relationships are resolved through associative tables: wallet-label (`wallet_label`), wallet-cluster
(`cluster_member`), snapshot edge-transaction (`snapshot_edge_tx`) and flag-transaction (`flag_evidence`). Every user is
in the single `app_user` table, with a `role` column.

## Known deviations

| Table and column | Dependency | Form affected | Why it is kept | Effect |
|---|---|---|---|---|
| `flag_evidence.block_number` | Key is `(flag_id, tx_hash)`, but `tx_hash → block_number`, also held in `transaction` | **2NF** | Evidence is read in block order, so this avoids a join with `transaction` on that read | Could disagree with `transaction` if written wrongly. On-chain block numbers do not change |
| `alert.run_id` when `cluster_id` is set | `cluster_id → run_id`, also held in `cluster` | **3NF** (only for cluster alerts) | Wallet alerts have no cluster, so `run_id` has to live here, and it is `NOT NULL` for every alert | A cluster alert could name a different run from its cluster |
| `framing_scenario.attacker_wallet_ids`, `target_wallet_ids` | Arrays of wallet ids | **1NF** | A log of one simulator run, never joined on, and removed by the simulator's `--cleanup` | No foreign key can check these ids |
| `flag_audit_tombstone.flag_id`, `verdict`, `row_hash` | `flag_audit_id →` each of them, and `flag_audit_id` is not declared unique | **3NF** unless `flag_audit_id` is treated as a candidate key | The record of a deleted audit row must keep its own copy, because the source row is gone | Written only by a trigger, once per deleted row |

## Controlled redundancy (not a normal-form violation)

These columns depend only on their own table's key, so they do not break 3NF, but they could be recomputed from other
tables and are stored for speed:

- `wallet.total_sent_wei`, `total_recv_wei`, `tx_count`
- `block.tx_count`
- `graph_snapshot.vertex_count`, `edge_count`
- `snapshot_edge.edge_weight`, `transfer_count`
- `cluster.member_count`, `internal_edges`
- `cluster_member.in_degree`, `out_degree`
- `framing_scenario.transfer_count`, `total_value_wei`
- `token.contract_address`, a copy of `wallet.address` for `contract_wallet_id`
