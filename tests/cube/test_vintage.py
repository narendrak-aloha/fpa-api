"""AS OF resolution against the live seeded dim_ledger_vintage table."""

import pytest

from fpa_be.cube.errors import NoVintageClosedYetError, UnknownVintageError
from fpa_be.cube.vintage import resolve_vintage
from fpa_be.dsl import ast_nodes as ast


def test_no_as_of_resolves_to_latest(ch_client):
    assert resolve_vintage(None, ch_client) is None


def test_timestamp_at_the_july_close_resolves_to_vintage_1(ch_client):
    assert resolve_vintage(ast.AsOf("2026-07-05T18:00:00"), ch_client) == 1


def test_timestamp_at_the_august_close_resolves_to_vintage_2(ch_client):
    assert resolve_vintage(ast.AsOf("2026-08-12T09:30:00"), ch_client) == 2


def test_timestamp_between_the_two_closes_resolves_to_the_earlier_vintage(ch_client):
    assert resolve_vintage(ast.AsOf("2026-07-20T00:00:00"), ch_client) == 1


def test_timestamp_before_any_close_is_rejected(ch_client):
    with pytest.raises(NoVintageClosedYetError):
        resolve_vintage(ast.AsOf("2020-01-01T00:00:00"), ch_client)


def test_bare_vintage_number_is_used_directly(ch_client):
    assert resolve_vintage(ast.AsOf(2.0), ch_client) == 2


def test_unknown_vintage_number_is_rejected(ch_client):
    with pytest.raises(UnknownVintageError):
        resolve_vintage(ast.AsOf(99.0), ch_client)
