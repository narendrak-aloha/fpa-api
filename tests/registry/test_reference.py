from fpa_be.registry.reference import (
    ACCOUNTS,
    ACCOUNTS_BY_CODE,
    COMPANIES,
    COMPANIES_BY_CODE,
    INTERCOMPANY_COST_ACCOUNT,
    VINTAGES,
)


def test_counts_match_seeded_cube():
    assert len(COMPANIES) == 20
    assert len(ACCOUNTS) == 25
    assert len(VINTAGES) == 2


def test_account_types_partition_correctly():
    by_type = {"Revenue": 0, "COGS": 0, "OpEx": 0}
    for a in ACCOUNTS:
        by_type[a.account_type] += 1
    assert by_type == {"Revenue": 7, "COGS": 8, "OpEx": 10}


def test_intercompany_account_is_cogs():
    assert ACCOUNTS_BY_CODE[INTERCOMPANY_COST_ACCOUNT].account_type == "COGS"


def test_lookup_dicts_are_keyed_correctly():
    assert ACCOUNTS_BY_CODE["41000"].account_name == "Services Revenue - Time and Materials"
    assert COMPANIES_BY_CODE["RTPL1"].functional_currency == "PLN"


def test_vintages_ordered_by_close_date():
    closed_ats = [v.closed_at for v in VINTAGES]
    assert closed_ats == sorted(closed_ats)
