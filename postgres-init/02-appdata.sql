-- The structured-data tool's own database (app/sql_store.py) — separate
-- from `litellm` (proxy state) and `langfuse` (trace UI state), so the
-- demo's own data has its own lifecycle and isn't sharing a schema with
-- infrastructure that happens to also live in this Postgres.
CREATE DATABASE appdata OWNER langfuse;

\connect appdata

-- Fixed, typed schema for app/tools.py::query_employees — a *fixed tool*,
-- never a text-to-SQL surface (GRAPH_PATTERNS.md pattern 21). `tenant`
-- mirrors the same column app/qdrant_store.py's payloads carry, enforced
-- the same way: every query app/sql_store.py issues is parameterized and
-- always includes `WHERE tenant = %s`, never a value the model supplies.
CREATE TABLE employees (
    id SERIAL PRIMARY KEY,
    tenant TEXT NOT NULL,
    name TEXT NOT NULL,
    department TEXT NOT NULL,
    title TEXT NOT NULL,
    hired_on DATE NOT NULL
);

CREATE INDEX employees_tenant_idx ON employees (tenant);

-- Two tenants seeded, `ecorp` (DEFAULT_TENANT, app/config.py) and a second
-- one purely so a cross-tenant isolation test has something real to prove
-- against — see tests/test_sql_store.py.
INSERT INTO employees (tenant, name, department, title, hired_on) VALUES
    ('ecorp', 'Priya Nair',      'Engineering', 'Staff Engineer',        '2021-03-01'),
    ('ecorp', 'Marcus Cole',     'Engineering', 'Engineering Manager',   '2020-11-15'),
    ('ecorp', 'Dana Whitfield',  'Support',     'Support Lead',          '2022-01-10'),
    ('ecorp', 'Sam Okafor',      'Support',     'Support Engineer',      '2023-06-01'),
    ('ecorp', 'Lena Fischer',    'Sales',       'Account Executive',     '2022-09-20'),
    ('other-co', 'Jordan Lee',  'Engineering', 'Software Engineer',     '2021-07-01');
