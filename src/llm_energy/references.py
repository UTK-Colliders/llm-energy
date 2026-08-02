"""Everyday energy reference points, for scale.

A run's energy in Joules means nothing to most readers. These convert it into
things people have intuitions about — a commute, a television left on — while
keeping the distinctions that make such comparisons honest rather than
rhetorical.

Two distinctions are carried on every reference and never collapsed:

**Basis.** Vehicle figures are the chemical energy of the fuel burned;
household figures are electricity delivered at the meter; the LLM estimate is
datacenter electricity including PUE. A kWh of electricity is not a kWh of
petrol — generating and delivering it costs roughly 2.6 kWh of primary energy
— so the two are only comparable once put on the same footing, which
`primary_joules` does explicitly rather than silently.

**Absolute vs avoidable.** A commute is energy consumed. An air conditioner
three degrees too cold, or a car on soft tyres, is energy *wasted* — the
difference between doing something well and doing it badly. "As much as a
commute" and "as much as the waste from soft tyres" are different claims, and
a plot that mixes them without saying so is arguing rather than reporting.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

KWH_J = 3.6e6
MJ_J = 1.0e6

# how a reference's energy is accounted
FUEL = "fuel (chemical)"
ELECTRICITY = "electricity (delivered)"

# whether it is energy spent or energy squandered
ABSOLUTE = "absolute"
AVOIDABLE = "avoidable"


@dataclass(frozen=True)
class Reference:
    id: str
    label: str
    joules: float
    basis: str                  # FUEL | ELECTRICITY
    kind: str                   # ABSOLUTE | AVOIDABLE
    detail: str = ""
    category: str = ""

    def primary_joules(self, factor: float) -> float:
        """Energy on a primary basis, so fuel and electricity are comparable.

        Fuel is already primary. Electricity is scaled up by the grid's
        generation and delivery losses.
        """
        return self.joules if self.basis == FUEL else self.joules * factor

    def as_kwh(self) -> float:
        return self.joules / KWH_J


def load_references(path: Path) -> tuple[list[Reference], dict]:
    """Build the reference table from its assumptions file.

    Returns (references, constants). The arithmetic is here rather than in the
    YAML so it can be tested and so each figure's derivation is readable.
    """
    import yaml

    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path}: not a mapping")
    c = data["constants"]
    mj_per_gal = float(c["gasoline_mj_per_gallon"])
    refs: list[Reference] = []

    # --- commuting ---------------------------------------------------------
    cm = data["commute"]
    miles = (float(cm["minutes_each_way"]) / 60.0
             * float(cm["average_speed_mph"]) * int(cm["trips_per_day"]))
    for key, v in cm["vehicles"].items():
        gallons = miles / float(v["mpg"])
        refs.append(Reference(
            id=f"commute-{key}",
            label=f"Commute, {v['label']}",
            joules=gallons * mj_per_gal * MJ_J,
            basis=FUEL, kind=ABSOLUTE, category="transport",
            detail=f"{miles:.0f} miles round trip at {v['mpg']:g} mpg "
                   f"({gallons:.2f} gal)"))

    # --- air conditioning three degrees too cold ---------------------------
    ac = data["air_conditioning"]
    extra_frac = float(ac["percent_per_degree_f"]) / 100.0 * int(ac["degrees_below"])
    per_day_kwh = float(ac["cooling_kwh_per_day"]) * extra_frac
    setpoint = int(ac["recommended_setpoint_f"])
    below = int(ac["degrees_below"])
    refs.append(Reference(
        id="ac-setpoint-day",
        label=f"AC {below} F below recommended, one day",
        joules=per_day_kwh * KWH_J,
        basis=ELECTRICITY, kind=AVOIDABLE, category="home",
        detail=f"{setpoint - below} F instead of {setpoint} F, "
               f"{extra_frac * 100:.0f}% more cooling on "
               f"{ac['cooling_kwh_per_day']:g} kWh/day"))
    refs.append(Reference(
        id="ac-setpoint-season",
        label=f"AC {below} F below recommended, one season",
        joules=per_day_kwh * int(ac["cooling_season_days"]) * KWH_J,
        basis=ELECTRICITY, kind=AVOIDABLE, category="home",
        detail=f"{ac['cooling_season_days']} days"))

    # --- soft tyres --------------------------------------------------------
    ty = data["tyres"]
    loss = (float(ty["percent_economy_loss_per_psi"]) / 100.0
            * float(ty["psi_below_recommended"]))
    mpg = float(cm["vehicles"][ty["vehicle"]]["mpg"])
    year_gal = float(ty["annual_miles"]) / mpg
    refs.append(Reference(
        id="tyres-year",
        label="Under-inflated tyres, one year of driving",
        joules=year_gal * loss * mj_per_gal * MJ_J,
        basis=FUEL, kind=AVOIDABLE, category="transport",
        detail=f"{ty['psi_below_recommended']:g} psi low ({loss * 100:.1f}% "
               f"worse economy) over {ty['annual_miles']:,} miles at "
               f"{mpg:g} mpg"))
    refs.append(Reference(
        id="tyres-commute",
        label="Under-inflated tyres, one commute",
        joules=(miles / mpg) * loss * mj_per_gal * MJ_J,
        basis=FUEL, kind=AVOIDABLE, category="transport",
        detail=f"the same {loss * 100:.1f}% over a {miles:.0f}-mile day"))

    # --- lights left on ----------------------------------------------------
    li = data["lighting"]
    hours, bulbs = float(li["hours"]), int(li["bulbs"])
    for key, watts, what in (("led", li["led_watts"], "LED"),
                             ("incandescent", li["incandescent_watts"],
                              "incandescent")):
        refs.append(Reference(
            id=f"lights-{key}",
            label=f"House lights on {hours:.0f} h ({what})",
            joules=bulbs * float(watts) * hours * 3600.0,
            basis=ELECTRICITY, kind=AVOIDABLE, category="home",
            detail=f"{bulbs} x {watts:g} W for {hours:.0f} h "
                   f"({bulbs * float(watts):.0f} W total)"))

    # --- television --------------------------------------------------------
    tv = data["television"]
    tv_hours = float(tv["hours"])
    refs.append(Reference(
        id="tv-65",
        label=f'65" TV on {tv_hours:.0f} h',
        joules=float(tv["watts"]) * tv_hours * 3600.0,
        basis=ELECTRICITY, kind=ABSOLUTE, category="home",
        detail=f"{tv['watts']:g} W for {tv_hours:.0f} h "
               f"(range {tv['low_watts']:g}-{tv['high_watts']:g} W)"))

    return refs, c


def compare(joules: float, refs: list[Reference], constants: dict,
            primary: bool = False) -> list[tuple[Reference, float]]:
    """Express `joules` as a fraction of each reference, largest first.

    With primary=True both sides are put on a primary-energy basis, which is
    the only way a litre of petrol and a kilowatt-hour off the grid belong on
    one axis.
    """
    factor = float(constants.get("primary_energy_factor", 1.0))
    # the measured quantity is itself electricity
    value = joules * factor if primary else joules
    out = []
    for r in refs:
        denom = r.primary_joules(factor) if primary else r.joules
        out.append((r, value / denom if denom > 0 else float("inf")))
    return sorted(out, key=lambda t: t[1])


def format_multiple(fraction: float) -> str:
    """'1/340 of' reads better than '0.0029x' at these ratios."""
    if fraction <= 0:
        return "0"
    if fraction >= 1:
        return f"{fraction:.3g}x"
    return f"1/{round(1 / fraction):,g}"
