"""resolve_dirty_set: the recompute workflow's step-2 dirty-dependent
resolution over the driver calc_order_dag."""

from fpa_be.dsl import parse_expr, resolve_dirty_set


def _parsed(formulas: dict[str, str]) -> dict[str, object]:
    return {name: parse_expr(formula) for name, formula in formulas.items()}


class TestResolveDirtySet:
    def test_shocked_driver_alone_is_dirty(self):
        formulas = _parsed({"utilisation": "1", "bill_rate": "1"})
        assert resolve_dirty_set(formulas, {"utilisation"}) == ["utilisation"]

    def test_direct_dependent_is_dirty(self):
        formulas = _parsed({
            "utilisation": "0.75",
            "revenue_forecast": "utilisation * bill_rate",
            "bill_rate": "150",
        })
        dirty = resolve_dirty_set(formulas, {"utilisation"})
        assert set(dirty) == {"utilisation", "revenue_forecast"}
        # topological order: utilisation must be recomputed before anything
        # that reads it.
        assert dirty.index("utilisation") < dirty.index("revenue_forecast")

    def test_transitive_dependents_are_dirty(self):
        formulas = _parsed({
            "a": "1",
            "b": "a * 2",
            "c": "b * 3",
            "unrelated": "42",
        })
        dirty = resolve_dirty_set(formulas, {"a"})
        assert set(dirty) == {"a", "b", "c"}
        assert dirty.index("a") < dirty.index("b") < dirty.index("c")

    def test_unrelated_drivers_are_left_alone(self):
        formulas = _parsed({
            "a": "1",
            "b": "a * 2",
            "unrelated": "42",
        })
        dirty = resolve_dirty_set(formulas, {"a"})
        assert "unrelated" not in dirty

    def test_diamond_dependency_each_dependent_appears_once(self):
        formulas = _parsed({
            "a": "1",
            "b": "a * 2",
            "c": "a * 3",
            "d": "b + c",
        })
        dirty = resolve_dirty_set(formulas, {"a"})
        assert dirty.count("d") == 1
        assert dirty.index("b") < dirty.index("d")
        assert dirty.index("c") < dirty.index("d")

    def test_multiple_shocked_drivers_in_one_run(self):
        formulas = _parsed({
            "a": "1",
            "b": "10",
            "downstream_of_a": "a * 2",
            "downstream_of_b": "b * 2",
            "unrelated": "0",
        })
        dirty = resolve_dirty_set(formulas, {"a", "b"})
        assert set(dirty) == {"a", "b", "downstream_of_a", "downstream_of_b"}
