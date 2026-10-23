-- LIVE DEMO (invalid 2/3): a foreign key pointing at a table that does not
-- exist. Postgres should refuse this outright.
--   python demo_schemas/demo_db.py load demo_schemas/live/invalid/02_broken_fk.sql
CREATE TABLE demo.invalid_broken_fk (
    id         SERIAL PRIMARY KEY,
    patient_id INT REFERENCES demo.nonexistent_table(id)
);
