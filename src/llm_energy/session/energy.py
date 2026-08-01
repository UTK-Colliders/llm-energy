"""Convert session token usage into an energy band via coefficients YAML."""

from __future__ import annotations

import fnmatch
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from llm_energy.schemas import (EnergyBand, ModelUsage, SessionEnergyResult,
                                SessionUsage)

LEVELS = ("low", "central", "high")
TOKEN_TYPES = ("input", "output", "cache_read", "cache_creation")


@dataclass
class ModelCoefficients:
    match: str
    per_token: dict[str, dict[str, float]]      # token_type -> level -> J
    per_request_overhead_j: dict[str, float]    # level -> J
    pue_included: bool = False
    sources: list[str] = field(default_factory=list)


@dataclass
class Coefficients:
    pue: float
    defaults: ModelCoefficients
    models: list[ModelCoefficients]
    file: str = ""
    sha256: str = ""

    def for_model(self, model: str) -> tuple[ModelCoefficients, bool]:
        """Return (coefficients, matched) — falls back to defaults."""
        for mc in self.models:
            if fnmatch.fnmatch(model, mc.match):
                return mc, True
        return self.defaults, False


def _parse_block(raw: dict, defaults: ModelCoefficients | None = None) -> ModelCoefficients:
    per_token = {}
    for tt in TOKEN_TYPES:
        if tt in raw:
            per_token[tt] = {lv: float(raw[tt][lv]) for lv in LEVELS}
        elif defaults is not None:
            per_token[tt] = dict(defaults.per_token[tt])
        else:
            raise ValueError(f"coefficients missing token type '{tt}'")
    if "per_request_overhead_j" in raw:
        overhead = {lv: float(raw["per_request_overhead_j"][lv]) for lv in LEVELS}
    elif defaults is not None:
        overhead = dict(defaults.per_request_overhead_j)
    else:
        overhead = {lv: 0.0 for lv in LEVELS}
    return ModelCoefficients(
        match=raw.get("match", "*"),
        per_token=per_token,
        per_request_overhead_j=overhead,
        pue_included=bool(raw.get("pue_included",
                                  defaults.pue_included if defaults else False)),
        sources=list(raw.get("sources", [])),
    )


def load_coefficients(path: Path) -> Coefficients:
    text = path.read_text()
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: not a mapping")
    if "defaults" not in data:
        raise ValueError(f"{path}: missing 'defaults' block")
    defaults = _parse_block(data["defaults"])
    models = []
    for m in data.get("models", []):
        if "match" not in m:
            raise ValueError(f"{path}: model entry missing 'match'")
        if not m.get("sources"):
            raise ValueError(f"{path}: model entry '{m['match']}' has no 'sources' "
                             "citations (required)")
        models.append(_parse_block(m, defaults=defaults))
    pue = float(data.get("pue", {}).get("value", 1.0))
    return Coefficients(
        pue=pue, defaults=defaults, models=models, file=str(path),
        sha256=hashlib.sha256(text.encode()).hexdigest(),
    )


def _model_band(m: ModelUsage, mc: ModelCoefficients, pue: float) -> EnergyBand:
    counts = {
        "input": m.input_tokens,
        "output": m.output_tokens,
        "cache_read": m.cache_read_tokens,
        "cache_creation": m.cache_creation_tokens,
    }
    vals = {}
    for lv in LEVELS:
        j = sum(counts[tt] * mc.per_token[tt][lv] for tt in TOKEN_TYPES)
        j += m.requests * mc.per_request_overhead_j[lv]
        if not mc.pue_included:
            j *= pue
        vals[lv] = j
    return EnergyBand(low_j=vals["low"], central_j=vals["central"], high_j=vals["high"])


def energy_band(usage: SessionUsage, coeffs: Coefficients) -> SessionEnergyResult:
    result = SessionEnergyResult(
        usage=usage,
        coefficients_file=coeffs.file,
        coefficients_sha256=coeffs.sha256,
        pue=coeffs.pue,
    )
    total = EnergyBand()
    for m in usage.per_model:
        mc, matched = coeffs.for_model(m.model)
        if not matched:
            result.notes.append(
                f"model '{m.model}' has no coefficients entry; used defaults")
        band = _model_band(m, mc, coeffs.pue)
        result.per_model_bands[m.model] = band
        total = total + band
    result.total_band = total
    result.notes.append(
        "band is a low/high envelope from literature estimates, not a "
        "statistical interval")
    return result
