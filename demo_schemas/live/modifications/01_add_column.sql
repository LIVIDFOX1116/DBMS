-- LIVE DEMO modification 1/5: add a column to an existing table.
--   python demo_schemas/demo_db.py load demo_schemas/live/modifications/01_add_column.sql
ALTER TABLE demo.customers ADD COLUMN phone VARCHAR(20);
