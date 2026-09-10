import hashlib
import importlib.util
from pathlib import Path

import pytest

from fpa_be.registry.dimensions import DIM_COLUMNS, SEPARATE_AXES, dim_signature


def test_dim_columns_is_alphabetical():
    assert list(DIM_COLUMNS) == sorted(DIM_COLUMNS)


def test_dim_columns_has_19_entries():
    assert len(DIM_COLUMNS) == 19


def test_separate_axes():
    assert SEPARATE_AXES == ("company", "account", "period_month")


def test_dim_signature_matches_manual_sha256():
    values = tuple(f"v{i}" for i in range(19))
    payload = "|".join(f"{d}={v}" for d, v in zip(DIM_COLUMNS, values))
    expected = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    assert dim_signature(values) == expected


def test_dim_signature_rejects_wrong_arity():
    with pytest.raises(ValueError):
        dim_signature(("only", "a", "few", "values"))


def test_dim_signature_matches_seed_fpa_byte_for_byte():
    """seed_fpa.py is frozen and never imported by app code -- this is the
    one place we load it directly, purely to prove the join key agrees."""
    seed_path = Path(__file__).resolve().parents[2] / "seed_fpa.py"
    spec = importlib.util.spec_from_file_location("seed_fpa", seed_path)
    seed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed)

    assert seed.DIM_COLUMNS == DIM_COLUMNS

    values = tuple(f"val-{i}" for i in range(19))
    assert seed.dim_signature(values) == dim_signature(values)
