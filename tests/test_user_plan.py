"""plan_params/user_plan: frescos por semilla, deterministas por semilla."""

from experiments.live_two_weeks import plan_params, user_plan


def test_params_deterministic():
    assert plan_params(7001, 7) == plan_params(7001, 7)


def test_params_vary_by_seed():
    seen = {tuple(sorted(plan_params(s, 7)["silent_days"])) for s in range(7001, 7011)}
    assert len(seen) > 1


def test_silent_double_disjoint_and_respected():
    params = plan_params(7001, 7)
    assert params["silent_days"].isdisjoint(params["double_days"])
    days = {e["day"] for e in user_plan(7001, 7, params)}
    assert days.isdisjoint(params["silent_days"])
    assert params["double_days"] <= days


def test_default_params_equal_explicit():
    assert user_plan(7001, 7) == user_plan(7001, 7, plan_params(7001, 7))
