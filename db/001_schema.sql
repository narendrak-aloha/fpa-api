-- FP&A governance database schema.
-- The ClickHouse cube remains the source of analytical facts.  This database
-- stores the governed inputs, workflow state, and audit evidence around it.

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE SCHEMA IF NOT EXISTS fpa_governance;
SET search_path TO fpa_governance, public;

CREATE TABLE IF NOT EXISTS app_user (
    user_id       text PRIMARY KEY,
    display_name   text NOT NULL,
    email          text NOT NULL UNIQUE,
    active         boolean NOT NULL DEFAULT true,
    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS role (
    role_code text PRIMARY KEY,
    description text NOT NULL
);

CREATE TABLE IF NOT EXISTS user_role (
    user_id text NOT NULL REFERENCES app_user(user_id),
    role_code text NOT NULL REFERENCES role(role_code),
    PRIMARY KEY (user_id, role_code)
);

CREATE TABLE IF NOT EXISTS dim_company (
    company_code text PRIMARY KEY,
    company_name text NOT NULL,
    country_code char(2) NOT NULL,
    region text NOT NULL,
    functional_currency char(3) NOT NULL,
    CHECK (country_code = upper(country_code)),
    CHECK (functional_currency = upper(functional_currency))
);

CREATE TABLE IF NOT EXISTS dim_account (
    account_code text PRIMARY KEY,
    account_name text NOT NULL,
    account_type text NOT NULL CHECK (account_type IN ('Revenue', 'COGS', 'OpEx')),
    engine_tag text NOT NULL CHECK (engine_tag IN ('Services', 'Recurring', 'Shared'))
);

CREATE TABLE IF NOT EXISTS dim_cost_center (
    cost_center_code text PRIMARY KEY,
    country_code char(2) NOT NULL,
    practice_code text NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger_vintage (
    vintage integer PRIMARY KEY CHECK (vintage > 0),
    closed_at timestamptz NOT NULL,
    note text NOT NULL
);

CREATE TABLE IF NOT EXISTS planning_model (
    model_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    model_code text NOT NULL UNIQUE,
    model_name text NOT NULL,
    plan_year integer NOT NULL CHECK (plan_year BETWEEN 2000 AND 2200),
    reporting_currency char(3) NOT NULL,
    calc_order_dag jsonb NOT NULL DEFAULT '[]'::jsonb,
    active boolean NOT NULL DEFAULT true,
    created_by text NOT NULL REFERENCES app_user(user_id),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS planning_dimension (
    model_id uuid NOT NULL REFERENCES planning_model(model_id) ON DELETE CASCADE,
    dimension_code text NOT NULL,
    ordinal smallint NOT NULL CHECK (ordinal > 0),
    PRIMARY KEY (model_id, dimension_code),
    UNIQUE (model_id, ordinal)
);

CREATE TABLE IF NOT EXISTS planning_measure (
    model_id uuid NOT NULL REFERENCES planning_model(model_id) ON DELETE CASCADE,
    measure_code text NOT NULL,
    aggregation_type text NOT NULL CHECK (aggregation_type IN ('additive', 'ratio', 'semi_additive')),
    sql_expression text NOT NULL,
    available boolean NOT NULL DEFAULT true,
    PRIMARY KEY (model_id, measure_code)
);

CREATE TABLE IF NOT EXISTS plan_driver (
    driver_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    model_id uuid NOT NULL REFERENCES planning_model(model_id),
    driver_code text NOT NULL,
    driver_name text NOT NULL,
    formula text NOT NULL,
    effective_from date NOT NULL,
    effective_to date,
    unit text NOT NULL,
    value_type text NOT NULL CHECK (value_type IN ('numeric', 'percentage', 'currency', 'count')),
    status text NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('DRAFT', 'ACTIVE', 'RETIRED')),
    created_by text NOT NULL REFERENCES app_user(user_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (model_id, driver_code, effective_from),
    CHECK (effective_to IS NULL OR effective_to > effective_from)
);

CREATE TABLE IF NOT EXISTS plan_state_transition (
    transition_id bigserial PRIMARY KEY,
    from_state text NOT NULL,
    to_state text NOT NULL,
    role_code text NOT NULL REFERENCES role(role_code),
    UNIQUE (from_state, to_state, role_code)
);

CREATE TABLE IF NOT EXISTS plan_version (
    plan_version_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    plan_version_code text NOT NULL UNIQUE,
    model_id uuid NOT NULL REFERENCES planning_model(model_id),
    plan_year integer NOT NULL CHECK (plan_year BETWEEN 2000 AND 2200),
    state text NOT NULL DEFAULT 'DRAFT' CHECK (state IN ('DRAFT', 'IN_REVIEW', 'APPROVED', 'LOCKED', 'SUPERSEDED', 'REJECTED')),
    covenant_ok boolean NOT NULL DEFAULT false,
    covenant_note text,
    requested_by text NOT NULL REFERENCES app_user(user_id),
    approved_by text REFERENCES app_user(user_id),
    supersedes_plan_version_id uuid REFERENCES plan_version(plan_version_id),
    revision integer NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (approved_by IS NULL OR approved_by <> requested_by),
    CHECK (state NOT IN ('APPROVED', 'LOCKED') OR covenant_ok),
    CHECK (state NOT IN ('APPROVED', 'LOCKED') OR approved_by IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS scenario_set (
    scenario_set_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    plan_version_id uuid NOT NULL REFERENCES plan_version(plan_version_id) ON DELETE CASCADE,
    scenario_code text NOT NULL CHECK (scenario_code ~ '^[a-z][a-z0-9_]*$'),
    scenario_name text NOT NULL,
    is_base boolean NOT NULL DEFAULT false,
    state text NOT NULL DEFAULT 'DRAFT' CHECK (state IN ('DRAFT', 'APPROVED', 'LOCKED')),
    UNIQUE (plan_version_id, scenario_code)
);

CREATE UNIQUE INDEX IF NOT EXISTS one_base_scenario_per_plan
    ON scenario_set(plan_version_id) WHERE is_base;

CREATE TABLE IF NOT EXISTS scenario_driver_override (
    scenario_set_id uuid NOT NULL REFERENCES scenario_set(scenario_set_id) ON DELETE CASCADE,
    driver_id uuid NOT NULL REFERENCES plan_driver(driver_id),
    override_value numeric(20,6) NOT NULL,
    rationale text NOT NULL,
    PRIMARY KEY (scenario_set_id, driver_id)
);

CREATE TABLE IF NOT EXISTS plan_fx_rate (
    plan_version_id uuid NOT NULL REFERENCES plan_version(plan_version_id) ON DELETE CASCADE,
    period_month date NOT NULL CHECK (period_month = date_trunc('month', period_month)::date),
    from_currency char(3) NOT NULL,
    to_currency char(3) NOT NULL DEFAULT 'USD',
    rate numeric(20,8) NOT NULL CHECK (rate > 0),
    PRIMARY KEY (plan_version_id, period_month, from_currency, to_currency)
);

CREATE TABLE IF NOT EXISTS plan_version_line (
    plan_line_id bigserial PRIMARY KEY,
    plan_version_id uuid NOT NULL REFERENCES plan_version(plan_version_id) ON DELETE CASCADE,
    scenario_code text NOT NULL,
    company_code text NOT NULL REFERENCES dim_company(company_code),
    period_month date NOT NULL CHECK (period_month = date_trunc('month', period_month)::date),
    account_code text NOT NULL REFERENCES dim_account(account_code),
    dim_signature_hash char(16) NOT NULL CHECK (dim_signature_hash ~ '^[0-9a-f]{16}$'),
    quantity numeric(20,6) NOT NULL,
    unit_price numeric(20,6) NOT NULL,
    amount_functional numeric(20,2) NOT NULL,
    functional_currency char(3) NOT NULL,
    driver_derivation_trace jsonb NOT NULL,
    source_revision integer NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (plan_version_id, scenario_code, company_code, period_month, account_code, dim_signature_hash),
    CHECK (jsonb_typeof(driver_derivation_trace) = 'object'),
    CHECK (driver_derivation_trace <> '{}'::jsonb),
    CHECK (amount_functional = round(quantity * unit_price, 2))
);

CREATE TABLE IF NOT EXISTS plan_approval (
    approval_id bigserial PRIMARY KEY,
    plan_version_id uuid NOT NULL REFERENCES plan_version(plan_version_id),
    requested_by text NOT NULL REFERENCES app_user(user_id),
    decided_by text REFERENCES app_user(user_id),
    decision text NOT NULL DEFAULT 'PENDING' CHECK (decision IN ('PENDING', 'APPROVED', 'REJECTED')),
    covenant_ok boolean NOT NULL,
    comment text,
    decided_at timestamptz,
    CHECK (decided_by IS NULL OR decided_by <> requested_by),
    CHECK ((decision = 'PENDING' AND decided_by IS NULL AND decided_at IS NULL) OR (decision <> 'PENDING' AND decided_by IS NOT NULL AND decided_at IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS variance_report (
    variance_report_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    plan_version_id uuid NOT NULL REFERENCES plan_version(plan_version_id),
    scenario_code text NOT NULL,
    as_of_vintage integer REFERENCES ledger_vintage(vintage),
    status text NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'REVIEWED', 'CLOSED')),
    created_by text NOT NULL REFERENCES app_user(user_id),
    closed_by text REFERENCES app_user(user_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    closed_at timestamptz,
    CHECK (status <> 'CLOSED' OR (closed_by IS NOT NULL AND closed_at IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS variance_report_line (
    variance_report_id uuid NOT NULL REFERENCES variance_report(variance_report_id) ON DELETE CASCADE,
    line_no integer NOT NULL CHECK (line_no > 0),
    dimension_key jsonb NOT NULL,
    plan_amount numeric(20,2) NOT NULL,
    actual_amount numeric(20,2) NOT NULL,
    price_variance numeric(20,2) NOT NULL DEFAULT 0,
    volume_variance numeric(20,2) NOT NULL DEFAULT 0,
    mix_variance numeric(20,2) NOT NULL DEFAULT 0,
    fx_variance numeric(20,2) NOT NULL DEFAULT 0,
    rate_variance numeric(20,2) NOT NULL DEFAULT 0,
    efficiency_variance numeric(20,2) NOT NULL DEFAULT 0,
    residual numeric(20,2) NOT NULL DEFAULT 0,
    PRIMARY KEY (variance_report_id, line_no),
    CHECK (jsonb_typeof(dimension_key) = 'object'),
    CHECK (round(actual_amount - plan_amount, 2) = round(price_variance + volume_variance + mix_variance + fx_variance + rate_variance + efficiency_variance + residual, 2))
);

CREATE TABLE IF NOT EXISTS llm_disclosure_log (
    disclosure_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id text NOT NULL REFERENCES app_user(user_id),
    scope jsonb NOT NULL,
    field_classes text[] NOT NULL,
    model_name text NOT NULL,
    payload_sha256 char(64) NOT NULL,
    response_sha256 char(64),
    disclosed_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS audit_event (
    audit_event_id bigserial PRIMARY KEY,
    occurred_at timestamptz NOT NULL DEFAULT now(),
    actor_user_id text REFERENCES app_user(user_id),
    entity_type text NOT NULL,
    entity_id text NOT NULL,
    action text NOT NULL,
    payload jsonb NOT NULL,
    previous_hash char(64),
    event_hash char(64) NOT NULL UNIQUE
);

CREATE OR REPLACE FUNCTION reject_locked_plan_write() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE current_state text;
BEGIN
    IF TG_TABLE_NAME = 'plan_version' THEN
        current_state := OLD.state;
    ELSE
        SELECT state INTO current_state FROM plan_version WHERE plan_version_id = OLD.plan_version_id;
    END IF;
    IF current_state = 'LOCKED' THEN
        RAISE EXCEPTION 'plan version is locked; create a superseding version instead';
    END IF;
    RETURN COALESCE(NEW, OLD);
END $$;

DROP TRIGGER IF EXISTS plan_version_lock_guard ON plan_version;
CREATE TRIGGER plan_version_lock_guard BEFORE UPDATE OR DELETE ON plan_version
FOR EACH ROW EXECUTE FUNCTION reject_locked_plan_write();

DROP TRIGGER IF EXISTS plan_line_lock_guard ON plan_version_line;
CREATE TRIGGER plan_line_lock_guard BEFORE UPDATE OR DELETE ON plan_version_line
FOR EACH ROW EXECUTE FUNCTION reject_locked_plan_write();

CREATE OR REPLACE FUNCTION validate_plan_approval() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.decision = 'APPROVED' AND NOT NEW.covenant_ok THEN
        RAISE EXCEPTION 'approval blocked: covenant is not satisfied';
    END IF;
    IF NEW.decision = 'APPROVED' AND NEW.decided_by = NEW.requested_by THEN
        RAISE EXCEPTION 'approval blocked: requester cannot approve their own plan';
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS plan_approval_guard ON plan_approval;
CREATE TRIGGER plan_approval_guard BEFORE INSERT OR UPDATE ON plan_approval
FOR EACH ROW EXECUTE FUNCTION validate_plan_approval();

CREATE OR REPLACE FUNCTION audit_event_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_event is append-only';
END $$;

DROP TRIGGER IF EXISTS audit_event_no_update ON audit_event;
CREATE TRIGGER audit_event_no_update BEFORE UPDATE OR DELETE ON audit_event
FOR EACH ROW EXECUTE FUNCTION audit_event_append_only();

CREATE INDEX IF NOT EXISTS plan_line_lookup_idx ON plan_version_line(plan_version_id, scenario_code, period_month, company_code, account_code);
CREATE INDEX IF NOT EXISTS driver_effective_idx ON plan_driver(model_id, driver_code, effective_from, effective_to);
CREATE INDEX IF NOT EXISTS audit_entity_idx ON audit_event(entity_type, entity_id, occurred_at);
