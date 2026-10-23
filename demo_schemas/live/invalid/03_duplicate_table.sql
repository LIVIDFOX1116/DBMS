-- LIVE DEMO (invalid 3/3): re-declaring a table name that already exists
-- (demo.students, from 01_student_management.sql) without IF NOT EXISTS.
-- Postgres should refuse this outright.
--   python demo_schemas/demo_db.py load demo_schemas/live/invalid/03_duplicate_table.sql
CREATE TABLE demo.students (
    id SERIAL PRIMARY KEY
);
