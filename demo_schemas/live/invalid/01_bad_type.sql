-- LIVE DEMO (invalid 1/3): an unknown column type. Postgres should refuse
-- this outright -- nothing gets created.
--   python demo_schemas/demo_db.py load demo_schemas/live/invalid/01_bad_type.sql
CREATE TABLE demo.invalid_bad_type (
    id     SERIAL PRIMARY KEY,
    amount NOTAREALTYPE
);
