"""Synthetic user tests (W-E1)."""

from harness.synth_user import SyntheticUser


def test_message_for_day_and_silence():
    user = SyntheticUser({0: "hi", 2: "yo"})
    assert user.message_for(0) == "hi"
    assert user.message_for(1) is None
    assert user.message_for(2) == "yo"


def test_good_month_has_all_days():
    user = SyntheticUser.good_month(days=30)
    for d in range(30):
        assert user.message_for(d) is not None


def test_bad_month_distinct_from_good():
    good = SyntheticUser.good_month(days=5)
    bad = SyntheticUser.bad_month(days=5)
    assert good.message_for(0) != bad.message_for(0)
