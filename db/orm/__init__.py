"""FrameCheck's ORM layer (SQLAlchemy 2.x).

The split this package exists to keep honest:

- **DDL lives in SQL.** db/schema.sql plus db/migrations/*.sql create and
  alter every table, applied by api/dbsetup.py. Nothing here ever calls
  ``metadata.create_all()`` / ``drop_all()``, and there is no Alembic
  autogenerate: the models describe tables that already exist, and
  tests/test_orm_models.py fails if they stop matching the DDL.
- **Data access goes through the ORM.** The API's CRUD and auth paths read
  and write through these models and sessions (see the "ORM coverage and
  permitted raw SQL" section of docs/API_CONTRACT.md for the exceptions and
  why each one is allowed).

Lives under db/ rather than api/ because graph/ uses the models too, and
graph/ must not depend on the API package.
"""
