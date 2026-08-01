from pathlib import Path

import pytest

from llm_energy.schemas import ModelUsage, SessionUsage
from llm_energy.session.energy import energy_band, load_coefficients

REPO = Path(__file__).parent.parent


def make_coeffs(tmp_path, pue_included=False, pue=2.0):
    text = f"""
schema_version: 1
units: joules_per_token
pue:
  value: {pue}
  source: test
defaults:
  input:          {{low: 1.0, central: 2.0, high: 3.0}}
  output:         {{low: 10.0, central: 20.0, high: 30.0}}
  cache_read:     {{low: 0.1, central: 0.2, high: 0.3}}
  cache_creation: {{low: 1.0, central: 2.0, high: 3.0}}
  per_request_overhead_j: {{low: 0.0, central: 5.0, high: 10.0}}
  pue_included: false
models:
  - match: "claude-*"
    inherit: defaults
    pue_included: {str(pue_included).lower()}
    sources: ["test source"]
"""
    p = tmp_path / "coeffs.yaml"
    p.write_text(text)
    return load_coefficients(p)


def usage_one_model(model="claude-fable-5"):
    return SessionUsage(per_model=[ModelUsage(
        model=model, input_tokens=10, output_tokens=5,
        cache_creation_tokens=2, cache_read_tokens=100, requests=2)])


def test_hand_computed_band_with_pue(tmp_path):
    coeffs = make_coeffs(tmp_path, pue_included=False, pue=2.0)
    res = energy_band(usage_one_model(), coeffs)
    # central: 10*2 + 5*20 + 100*0.2 + 2*2 + 2 requests*5 = 154; x PUE 2 = 308
    assert res.total_band.central_j == pytest.approx(308.0)
    # low: (10*1 + 5*10 + 100*0.1 + 2*1 + 0) * 2 = 144
    assert res.total_band.low_j == pytest.approx(144.0)
    # high: (10*3 + 5*30 + 100*0.3 + 2*3 + 2*10) * 2 = 472
    assert res.total_band.high_j == pytest.approx(472.0)
    assert res.total_band.low_j <= res.total_band.central_j <= res.total_band.high_j


def test_pue_included_skips_multiplier(tmp_path):
    coeffs = make_coeffs(tmp_path, pue_included=True, pue=2.0)
    res = energy_band(usage_one_model(), coeffs)
    assert res.total_band.central_j == pytest.approx(154.0)


def test_unknown_model_uses_defaults_with_note(tmp_path):
    coeffs = make_coeffs(tmp_path)
    res = energy_band(usage_one_model(model="gpt-x"), coeffs)
    assert any("no coefficients entry" in n for n in res.notes)
    assert res.total_band.central_j > 0


def test_missing_sources_rejected(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("""
defaults:
  input:          {low: 1, central: 1, high: 1}
  output:         {low: 1, central: 1, high: 1}
  cache_read:     {low: 1, central: 1, high: 1}
  cache_creation: {low: 1, central: 1, high: 1}
models:
  - match: "claude-*"
""")
    with pytest.raises(ValueError, match="sources"):
        load_coefficients(p)


def test_shipped_default_coefficients_load():
    coeffs = load_coefficients(REPO / "coefficients" / "default.yaml")
    assert coeffs.pue > 1.0
    mc, matched = coeffs.for_model("claude-fable-5")
    assert matched
    assert mc.sources
    # sanity: output tokens strictly costlier than cache reads at every level
    for lv in ("low", "central", "high"):
        assert mc.per_token["output"][lv] > mc.per_token["cache_read"][lv]
    assert coeffs.sha256 and len(coeffs.sha256) == 64


def test_energy_band_wh_conversion(tmp_path):
    coeffs = make_coeffs(tmp_path)
    res = energy_band(usage_one_model(), coeffs)
    low_wh, central_wh, high_wh = res.total_band.as_wh()
    assert central_wh == pytest.approx(res.total_band.central_j / 3600.0)
