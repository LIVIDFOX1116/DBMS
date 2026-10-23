-- LIVE DEMO modification 3/5: add a constraint to an existing column.
--   python demo_schemas/demo_db.py load demo_schemas/live/modifications/03_add_constraint.sql
ALTER TABLE demo.customers ADD CONSTRAINT uq_customer_phone UNIQUE (phone_number);
