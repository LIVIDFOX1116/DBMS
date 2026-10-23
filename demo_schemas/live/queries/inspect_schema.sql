-- Reusable before/after proof query: run this exact file before AND after
-- each modification step to show the column/constraint/table list changed.
-- Two result sets: [1] every column of demo.customers, [2] every table
-- currently in the `demo` schema.

SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_schema = 'demo' AND table_name = 'customers'
ORDER BY ordinal_position;

SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'demo'
ORDER BY table_name;
