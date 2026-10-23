-- LIVE DEMO modification 2/5: rename a column.
--   python demo_schemas/demo_db.py load demo_schemas/live/modifications/02_rename_column.sql
ALTER TABLE demo.customers RENAME COLUMN phone TO phone_number;
