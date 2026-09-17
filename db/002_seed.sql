-- Deterministic governance seed for the supplied cube manifest.
SET search_path TO fpa_governance, public;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

INSERT INTO role(role_code, description) VALUES
 ('analyst','Read governed plans and analytical results'),
 ('planner','Create and revise draft plans'),
 ('controller','Maintain rates/covenants and approve plans'),
 ('cfo','Approve and lock plans'),
 ('service','Service identity for workflow publication')
ON CONFLICT (role_code) DO NOTHING;

INSERT INTO app_user(user_id, display_name, email) VALUES
 ('u-planner','Priya Planner','priya.planner@realtech.example'),
 ('u-controller','Chris Controller','chris.controller@realtech.example'),
 ('u-cfo','Casey CFO','casey.cfo@realtech.example'),
 ('svc-temporal','Temporal Workflow','temporal@realtech.example')
ON CONFLICT (user_id) DO NOTHING;

INSERT INTO user_role(user_id, role_code) VALUES
 ('u-planner','planner'), ('u-planner','analyst'),
 ('u-controller','controller'), ('u-controller','analyst'),
 ('u-cfo','cfo'), ('u-cfo','controller'),
 ('svc-temporal','service')
ON CONFLICT DO NOTHING;

INSERT INTO dim_company(company_code, company_name, country_code, region, functional_currency) VALUES
 ('RTUS1','RealTech US Operations 1','US','AMER','USD'), ('RTUS2','RealTech US Operations 2','US','AMER','USD'), ('RTUS3','RealTech US Operations 3','US','AMER','USD'),
 ('RTCA1','RealTech CA Operations 1','CA','AMER','CAD'), ('RTCA2','RealTech CA Operations 2','CA','AMER','CAD'),
 ('RTUK1','RealTech UK Operations 1','UK','EMEA','GBP'), ('RTUK2','RealTech UK Operations 2','UK','EMEA','GBP'),
 ('RTDE1','RealTech DE Operations 1','DE','EMEA','EUR'), ('RTDE2','RealTech DE Operations 2','DE','EMEA','EUR'),
 ('RTPL1','RealTech PL Operations 1','PL','EMEA','PLN'), ('RTPL2','RealTech PL Operations 2','PL','EMEA','PLN'), ('RTPL3','RealTech PL Operations 3','PL','EMEA','PLN'),
 ('RTAE1','RealTech AE Operations 1','AE','EMEA','AED'),
 ('RTIN1','RealTech IN Operations 1','IN','APAC','INR'), ('RTIN2','RealTech IN Operations 2','IN','APAC','INR'), ('RTIN3','RealTech IN Operations 3','IN','APAC','INR'),
 ('RTSG1','RealTech SG Operations 1','SG','APAC','SGD'), ('RTSG2','RealTech SG Operations 2','SG','APAC','SGD'),
 ('RTAU1','RealTech AU Operations 1','AU','APAC','AUD'), ('RTAU2','RealTech AU Operations 2','AU','APAC','AUD')
ON CONFLICT (company_code) DO NOTHING;

INSERT INTO dim_account(account_code, account_name, account_type, engine_tag) VALUES
 ('41000','Services Revenue - Time and Materials','Revenue','Services'), ('41010','Services Revenue - Fixed Fee','Revenue','Services'), ('41020','Services Revenue - Change Orders','Revenue','Services'),
 ('41100','Subscription Revenue','Revenue','Recurring'), ('41200','Usage Revenue','Revenue','Recurring'), ('41300','Support and Maintenance Revenue','Revenue','Recurring'), ('41400','Rebillable Expense Revenue','Revenue','Services'),
 ('51000','Delivery Payroll','COGS','Services'), ('51050','Delivery Bonus and Incentive','COGS','Services'), ('51100','Subcontractor Cost','COGS','Services'), ('51200','Cloud Hosting','COGS','Recurring'), ('51250','Third Party Software - Resold','COGS','Recurring'), ('51300','Rebillable Travel','COGS','Services'), ('51400','Customer Support Payroll','COGS','Recurring'), ('51500','Intercompany Delivery Cost','COGS','Shared'),
 ('61000','Sales Payroll','OpEx','Shared'), ('61100','Marketing Programs','OpEx','Shared'), ('61200','Sales Commission','OpEx','Shared'), ('62000','Research and Development Payroll','OpEx','Recurring'), ('62100','Product Tooling','OpEx','Recurring'), ('63000','General and Administrative Payroll','OpEx','Shared'), ('63100','Facilities and Occupancy','OpEx','Shared'), ('63200','Professional Fees','OpEx','Shared'), ('63300','Software Subscriptions','OpEx','Shared'), ('64000','Depreciation and Amortisation','OpEx','Shared')
ON CONFLICT (account_code) DO NOTHING;

INSERT INTO dim_cost_center(cost_center_code, country_code, practice_code)
SELECT format('CC-%s-%s', c, p), c, p
FROM (VALUES ('AE'),('AU'),('CA'),('DE'),('IN'),('PL'),('SG'),('UK'),('US')) countries(c)
 CROSS JOIN (VALUES ('CLOU'),('CYBE'),('DATA'),('ERPD'),('MANA'),('PROD')) practices(p)
ON CONFLICT (cost_center_code) DO NOTHING;

INSERT INTO ledger_vintage(vintage, closed_at, note) VALUES
 (1,'2026-07-05T18:00:00Z','original Q2 close'),
 (2,'2026-08-12T09:30:00Z','Q2 restatement')
ON CONFLICT (vintage) DO NOTHING;

INSERT INTO planning_model(model_code, model_name, plan_year, reporting_currency, calc_order_dag, created_by)
VALUES ('FPA-2026','RealTech FY2026 Reforecast',2026,'USD',
 '[{"driver":"heads","depends_on":[]},{"driver":"available_hours","depends_on":["heads"]},{"driver":"utilisation","depends_on":["available_hours"]},{"driver":"bill_rate","depends_on":[]},{"driver":"realisation","depends_on":["bill_rate"]},{"driver":"attach_rate","depends_on":["heads"]}]'::jsonb,
 'u-controller')
ON CONFLICT (model_code) DO NOTHING;

INSERT INTO planning_dimension(model_id, dimension_code, ordinal)
SELECT model_id, dimension_code, ordinal
FROM planning_model CROSS JOIN (VALUES
 ('billing_type',1),('business_unit',2),('channel',3),('contract',4),('cost_center',5),('cost_pool',6),('customer',7),('delivery_shore',8),('engine',9),('funding_source',10),('geo_country',11),('geo_region',12),('grade',13),('intercompany_flag',14),('practice',15),('product',16),('project',17),('resource_employee',18),('revenue_type',19)
) d(dimension_code, ordinal)
WHERE model_code = 'FPA-2026' ON CONFLICT DO NOTHING;

INSERT INTO planning_measure(model_id, measure_code, aggregation_type, sql_expression, available)
SELECT model_id, m.measure_code, m.aggregation_type, m.sql_expression, m.available
FROM planning_model CROSS JOIN (VALUES
 ('services_revenue','additive','sumIf(amount_functional, account IN (''41000'',''41010'',''41020'',''41400''))',true),
 ('delivery_cost','additive','sumIf(amount_functional, account IN (''51000'',''51050'',''51100'',''51300'',''51500''))',true),
 ('gross_margin','additive','services_revenue - delivery_cost',true),
 ('utilisation','ratio','billable_quantity / nullIf(delivery_quantity, 0)',true),
 ('realisation','ratio','revenue_amount / nullIf(billable_quantity, 0)',true),
 ('gross_margin_pct','ratio','gross_margin / nullIf(services_revenue, 0)',true),
 ('headcount','semi_additive','uniq(resource_employee)',true),
 ('open_pipeline','semi_additive','0',false)
) m(measure_code, aggregation_type, sql_expression, available)
WHERE model_code = 'FPA-2026' ON CONFLICT DO NOTHING;

INSERT INTO plan_driver(model_id, driver_code, driver_name, formula, effective_from, unit, value_type, created_by)
SELECT model_id, d.driver_code, d.driver_name, d.formula, '2026-01-01', d.unit, d.value_type, 'u-controller'
FROM planning_model CROSS JOIN (VALUES
 ('heads','Delivery heads','heads * (1 - attrition)','people','count'),
 ('available_hours','Available delivery hours','heads * 160','hours','count'),
 ('utilisation','Delivery utilisation','billable_hours / available_hours','ratio','percentage'),
 ('bill_rate','Blended bill rate','prior_period(bill_rate) * (1 + rate_increase)','USD/hour','currency'),
 ('realisation','Revenue realisation','bill_rate * realisation_factor','ratio','percentage'),
 ('attach_rate','Support attach rate','support_customers / customers','ratio','percentage')
) d(driver_code, driver_name, formula, unit, value_type)
WHERE model_code = 'FPA-2026' ON CONFLICT (model_id, driver_code, effective_from) DO NOTHING;

INSERT INTO plan_state_transition(from_state, to_state, role_code) VALUES
 ('DRAFT','IN_REVIEW','planner'), ('IN_REVIEW','APPROVED','controller'), ('IN_REVIEW','REJECTED','controller'),
 ('APPROVED','LOCKED','cfo'), ('APPROVED','SUPERSEDED','planner'), ('REJECTED','DRAFT','planner')
ON CONFLICT DO NOTHING;

INSERT INTO plan_version(plan_version_code, model_id, plan_year, state, covenant_ok, covenant_note, requested_by)
SELECT 'PV-2026-0001', model_id, 2026, 'DRAFT', false, 'Seed version awaiting covenant review', 'u-planner'
FROM planning_model WHERE model_code = 'FPA-2026'
ON CONFLICT (plan_version_code) DO NOTHING;

INSERT INTO scenario_set(plan_version_id, scenario_code, scenario_name, is_base)
SELECT plan_version_id, s.scenario_code, s.scenario_name, s.is_base
FROM plan_version CROSS JOIN (VALUES ('base','Base plan',true),('stretch','Stretch case',false),('downside','Downside case',false)) s(scenario_code, scenario_name, is_base)
WHERE plan_version_code = 'PV-2026-0001' ON CONFLICT DO NOTHING;

INSERT INTO plan_fx_rate(plan_version_id, period_month, from_currency, rate)
SELECT plan_version_id, make_date(2026, month_no, 1), currency_code, rate
FROM plan_version CROSS JOIN (VALUES
 ('CAD',1.36::numeric),('GBP',0.79::numeric),('EUR',0.92::numeric),('PLN',4.05::numeric),('AED',3.6725::numeric),('INR',83.00::numeric),('SGD',1.34::numeric),('AUD',1.52::numeric),('USD',1.00::numeric)
) fx(currency_code, rate) CROSS JOIN generate_series(1,12) months(month_no)
WHERE plan_version_code = 'PV-2026-0001' ON CONFLICT DO NOTHING;

INSERT INTO audit_event(actor_user_id, entity_type, entity_id, action, payload, previous_hash, event_hash)
VALUES ('u-controller','plan_version','PV-2026-0001','SEEDED', '{"source":"out/cube_manifest.json","seed":"002_seed.sql"}'::jsonb, NULL,
 encode(digest('u-controller|plan_version|PV-2026-0001|SEEDED|{""source"":""out/cube_manifest.json"",""seed"":""002_seed.sql""}', 'sha256'),'hex'))
ON CONFLICT (event_hash) DO NOTHING;
