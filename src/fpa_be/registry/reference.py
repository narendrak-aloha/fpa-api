"""Frozen reference data: companies, chart of accounts, ledger vintages.

Values are copied from out/cube_manifest.json (written by seed_fpa.py, which
is deterministic for a given --seed). out/ is a build artifact and is not
committed, so this module freezes the contract into code instead of reading
the manifest at import time -- a fresh clone has this before it ever runs the
seed script.
"""

from dataclasses import dataclass

PLAN_VERSION = "PV-2026-0001"
PLAN_YEAR = 2026
GROUP_REPORTING_CURRENCY = "USD"
INTERCOMPANY_COST_ACCOUNT = "51500"


@dataclass(frozen=True)
class Company:
    company: str
    company_name: str
    country_code: str
    region: str
    functional_currency: str


@dataclass(frozen=True)
class Account:
    account: str
    account_name: str
    account_type: str  # "Revenue" | "COGS" | "OpEx"
    engine_tag: str  # "Services" | "Recurring" | "Shared"


@dataclass(frozen=True)
class Vintage:
    vintage: int
    closed_at: str  # ISO timestamp
    note: str


COMPANIES: tuple[Company, ...] = (
    Company("RTUS1", "RealTech US Operations 1", "US", "AMER", "USD"),
    Company("RTUS2", "RealTech US Operations 2", "US", "AMER", "USD"),
    Company("RTUS3", "RealTech US Operations 3", "US", "AMER", "USD"),
    Company("RTCA1", "RealTech CA Operations 1", "CA", "AMER", "CAD"),
    Company("RTCA2", "RealTech CA Operations 2", "CA", "AMER", "CAD"),
    Company("RTUK1", "RealTech UK 1", "UK", "EMEA", "GBP"),
    Company("RTUK2", "RealTech UK 2", "UK", "EMEA", "GBP"),
    Company("RTDE1", "RealTech DE 1", "DE", "EMEA", "EUR"),
    Company("RTDE2", "RealTech DE 2", "DE", "EMEA", "EUR"),
    Company("RTPL1", "RealTech PL 1", "PL", "EMEA", "PLN"),
    Company("RTPL2", "RealTech PL 2", "PL", "EMEA", "PLN"),
    Company("RTPL3", "RealTech PL 3", "PL", "EMEA", "PLN"),
    Company("RTAE1", "RealTech AE Operations 1", "AE", "EMEA", "AED"),
    Company("RTIN1", "RealTech IN Operations 1", "IN", "APAC", "INR"),
    Company("RTIN2", "RealTech IN Operations 2", "IN", "APAC", "INR"),
    Company("RTIN3", "RealTech IN Operations 3", "IN", "APAC", "INR"),
    Company("RTSG1", "RealTech SG Operations 1", "SG", "APAC", "SGD"),
    Company("RTSG2", "RealTech SG Operations 2", "SG", "APAC", "SGD"),
    Company("RTAU1", "RealTech AU Operations 1", "AU", "APAC", "AUD"),
    Company("RTAU2", "RealTech AU Operations 2", "AU", "APAC", "AUD"),
)

ACCOUNTS: tuple[Account, ...] = (
    Account("41000", "Services Revenue - Time and Materials", "Revenue", "Services"),
    Account("41010", "Services Revenue - Fixed Fee", "Revenue", "Services"),
    Account("41020", "Services Revenue - Change Orders", "Revenue", "Services"),
    Account("41100", "Subscription Revenue", "Revenue", "Recurring"),
    Account("41200", "Usage Revenue", "Revenue", "Recurring"),
    Account("41300", "Support and Maintenance Revenue", "Revenue", "Recurring"),
    Account("41400", "Rebillable Expense Revenue", "Revenue", "Services"),
    Account("51000", "Delivery Payroll", "COGS", "Services"),
    Account("51050", "Delivery Bonus and Incentive", "COGS", "Services"),
    Account("51100", "Subcontractor Cost", "COGS", "Services"),
    Account("51200", "Cloud Hosting", "COGS", "Recurring"),
    Account("51250", "Third Party Software - Resold", "COGS", "Recurring"),
    Account("51300", "Rebillable Travel", "COGS", "Services"),
    Account("51400", "Customer Support Payroll", "COGS", "Recurring"),
    Account("61000", "Sales Payroll", "OpEx", "Shared"),
    Account("61100", "Marketing Programs", "OpEx", "Shared"),
    Account("61200", "Sales Commission", "OpEx", "Shared"),
    Account("62000", "Research and Development Payroll", "OpEx", "Recurring"),
    Account("62100", "Product Tooling", "OpEx", "Recurring"),
    Account("63000", "General and Administrative Payroll", "OpEx", "Shared"),
    Account("63100", "Facilities and Occupancy", "OpEx", "Shared"),
    Account("63200", "Professional Fees", "OpEx", "Shared"),
    Account("63300", "Software Subscriptions", "OpEx", "Shared"),
    Account("64000", "Depreciation and Amortisation", "OpEx", "Shared"),
    Account("51500", "Intercompany Delivery Cost", "COGS", "Shared"),
)

VINTAGES: tuple[Vintage, ...] = (
    Vintage(1, "2026-07-05T18:00:00", "original Q2 close"),
    Vintage(2, "2026-08-12T09:30:00", "Q2 restatement"),
)

ACCOUNTS_BY_CODE: dict[str, Account] = {a.account: a for a in ACCOUNTS}
COMPANIES_BY_CODE: dict[str, Company] = {c.company: c for c in COMPANIES}
