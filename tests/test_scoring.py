from erranderpy.core.llm.pricing import cost_usd
from erranderpy.evals.runner import load_cases
from erranderpy.evals.scoring import match, normalize, percentile, same_agents, tr_lower  # noqa: F401


def test_turkish_lowercase():
    assert tr_lower("IĞDIR") == "ığdır"
    assert tr_lower("İNGİLİZCE") == "ingilizce"
    assert normalize("  Zeytin   Yağı ") == "zeytin yağı"


def test_strings_lists_and_dicts():
    assert match({"names": ["Makarna"]}, {"names": ["makarna"]})
    assert match(["a", "b"], ["b", "a"])  # order doesn't matter
    assert not match(["a"], ["a", "b"])  # but length does
    assert not match(["a", "a"], ["a", "b"])  # each actual item is used once
    assert match({"items": [{"name": "süt"}]}, {"items": [{"name": "süt", "quantity": 1}]})  # extra keys ok
    assert not match({"items": [{"name": "süt", "quantity": 2}]}, {"items": [{"name": "süt"}]})


def test_contains_and_numbers():
    assert match({"title": "~fatura"}, {"title": "Elektrik faturasını öde"})
    assert not match({"title": "~fatura"}, {"title": "kirayı öde"})
    assert match({"minutes": 60}, {"minutes": 60.0})
    assert not match({"minutes": 60}, {"minutes": "60"})  # schema says integer
    assert match({}, {"anything": 1})


def test_router_agents_are_a_multiset():
    assert same_agents(["shopping", "tasks"], ["tasks", "shopping"])
    assert not same_agents(["shopping"], ["shopping", "shopping"])


def test_percentile():
    assert percentile([], 50) is None
    assert percentile([5, 1, 3], 50) == 3
    assert percentile(list(range(1, 101)), 95) == 95


def test_unknown_model_has_no_price():
    assert cost_usd("some-new-model", 100, 100) is None


def test_eval_datasets_load_and_ids_are_unique():
    for suite, minimum in (("router", 31), ("tools", 22)):
        cases = load_cases(suite)
        assert len(cases) >= minimum
        ids = [c["id"] for c in cases]
        assert len(ids) == len(set(ids))
