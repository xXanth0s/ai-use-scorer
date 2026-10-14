import pytest

from scorer import config

from scorer.prices import context_window, cost_usd, lookup, normalize


def test_unknown_model_is_none_not_zero():
    assert cost_usd("totally-unknown", 1000, 0, 0, 1000) is None
    assert cost_usd(None, 1000, 0, 0, 1000) is None
    assert cost_usd("<synthetic>", 1, 1, 1, 1) is None
    assert context_window("totally-unknown") is None


def test_1m_suffix():
    assert normalize("claude-sonnet-4-5[1m]") == ("claude-sonnet-4-5", True)
    assert context_window("claude-sonnet-4-5[1m]") == 1_000_000
    assert context_window("claude-sonnet-4-5") == 200_000
    assert context_window("claude-something-new[1m]") == 1_000_000


def test_window_bumps_when_usage_exceeds():
    assert context_window("claude-haiku-4-5", tokens=250_000) == 1_000_000
    assert context_window("claude-unknown-9") == 200_000


@pytest.mark.parametrize("model,key_input", [
    ("claude-opus-5-5", 4.0),
    ("claude-opus-5", 5.0),          # must not match opus-5-5
    ("claude-opus-4-6", 5.0),
    ("claude-sonnet-5-5", 2.0),
    ("claude-sonnet-4-5-20250929", 3.0),  # date suffix stripped
    ("anthropic.claude-opus-4-1-20250805-v1:0", 15.0),
    ("claude-fable-5-1", 10.0),
    ("claude-haiku-5-5", 0.10),
    ("gpt-6-astra", 10.0),
    ("gpt-6.1-sol", 2.0),
])
def test_lookup(model, key_input):
    assert lookup(model).input == key_input


def test_cost_math():
    # opus-5-5: 4 in, 0.20 cache read, 5 write, 20 out per 1M
    c = cost_usd("claude-opus-5-5", 1_000_000, 1_000_000, 1_000_000, 1_000_000)
    assert c == pytest.approx(4 + 0.2 + 5 + 20)
    assert cost_usd("claude-opus-5-5", 0, 0, 0, 0) == 0.0


def test_gpt_window():
    assert context_window("gpt-6-astra") == 258_400


@pytest.fixture(autouse=True)
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("SCORER_HOME", str(tmp_path))
    config.load.cache_clear()

    def put(text):
        (tmp_path / "config.toml").write_text(text)
        config.load.cache_clear()
    yield put
    config.load.cache_clear()


def test_config_override_exact_then_base(cfg):
    cfg('[prices."claude-opus-5-5"]\ninput = 5.0\ncached = 0.5\ncache_write = 6.25\noutput = 25.0\n'
        '[prices."claude-sonnet-4-5"]\ninput = 1.0\noutput = 2.0\n'
        '[prices."my-local-model"]\ninput = 1.0\noutput = 1.0\n')
    assert cost_usd("claude-opus-5-5", 1_000_000, 0, 0, 1_000_000) == pytest.approx(30.0)
    p = lookup("claude-sonnet-4-5-20250929")           # base-id match; missing keys from table
    assert (p.input, p.output, p.cache_read) == (1.0, 2.0, 0.30)
    assert cost_usd("my-local-model", 1_000_000, 0, 0, 0) == pytest.approx(1.0)   # not in table
    assert cost_usd("claude-opus-5", 1_000_000, 0, 0, 0) == pytest.approx(5.0)    # untouched


def test_no_config_uses_table(cfg):
    assert lookup("claude-opus-5-5").input == 4.0
