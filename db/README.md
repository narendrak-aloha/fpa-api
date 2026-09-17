# Postgres governance store

Apply the schema and seed in order:

```bash
psql "$DATABASE_URL" -f db/001_schema.sql
psql "$DATABASE_URL" -f db/002_seed.sql
```

The database is intentionally separate from the ClickHouse cube. ClickHouse
holds large actual/plan facts; Postgres holds the trusted control plane: model
registry, effective-dated drivers, plan state, scenario overrides, approvals,
FX assumptions, variance evidence, disclosure metadata, and audit history.

Database-level protections include locked-plan write guards, non-empty driver
derivation traces, amount = quantity × unit price, segregated approval, stored
covenant gating, and append-only audit events. Application code still owns
formula parsing, workflow orchestration, hash-chain verification, and the
ClickHouse publication transaction.
