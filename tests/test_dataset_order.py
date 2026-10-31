"""Order matters for flag_audit in a dataset -- offline.

Importing a dataset COPYs flag_audit rows through migration 009's chain
trigger, which links each new row to the current highest-id row. Rows written
out of id order therefore import as a broken chain (found in a round trip:
the export came out 11, 12, 13, 1, ... and verification failed at row 1).
"""
import re

from scripts import export_dataset


def test_flag_audit_is_exported_in_id_order():
    query = " ".join(dict(export_dataset.TABLES)["flag_audit"].split())
    assert re.search(r"ORDER BY fa\.flag_audit_id\s*$", query), query
