-- LIVE DEMO modification 5/5: drop a table (the one 04_add_table.sql just
-- created -- demonstrates DROP cleanly, safe because it's in the isolated
-- `demo` namespace).
--   python demo_schemas/demo_db.py load demo_schemas/live/modifications/05_drop_table.sql
DROP TABLE demo.product_reviews;
