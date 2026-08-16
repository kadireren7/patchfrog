from tests.fixtures.dogfood.context_pr_validation_example import dispatch


def test_dispatch():
    assert dispatch("1 + 1") == 2
