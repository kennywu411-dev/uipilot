from uipilot.calc import calculate


def test_large_multiplication_is_exact():
    # Sonnet 两次都算成 41,205.995 的那个数
    assert "41204.995" in calculate("316961.50 * 13%") and "41,205.00" in calculate("316961.50 * 0.13")


def test_rejects_anything_but_arithmetic():
    import pytest

    for bad in ["__import__('os')", "2 ** 10", "a + 1"]:
        with pytest.raises(ValueError):
            calculate(bad)
