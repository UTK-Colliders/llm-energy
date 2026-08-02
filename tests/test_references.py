"""Everyday energy references: the arithmetic, and the distinctions it keeps."""

from pathlib import Path

import pytest
import yaml

from llm_energy.references import (ABSOLUTE, AVOIDABLE, ELECTRICITY, FUEL,
                                   KWH_J, MJ_J, compare, format_multiple,
                                   load_references)

SPEC = Path(__file__).parent.parent / "references" / "everyday.yaml"


@pytest.fixture
def refs():
    rs, c = load_references(SPEC)
    return {r.id: r for r in rs}, c


def test_commute_energy_is_miles_over_mpg_times_fuel_content(refs):
    rs, _ = refs
    # 30 min each way at 30 mph, both ways -> 30 miles
    assert rs["commute-pickup"].joules == pytest.approx(30 / 20 * 120 * MJ_J)
    assert rs["commute-sedan"].joules == pytest.approx(30 / 35 * 120 * MJ_J)
    assert rs["commute-hybrid"].joules == pytest.approx(30 / 55 * 120 * MJ_J)


def test_the_three_vehicles_rank_as_expected(refs):
    rs, _ = refs
    assert (rs["commute-hybrid"].joules < rs["commute-sedan"].joules
            < rs["commute-pickup"].joules)


def test_ac_setpoint_is_the_extra_cooling_not_the_total(refs):
    rs, _ = refs
    # 3 F x 3%/F = 9% of 20 kWh/day
    assert rs["ac-setpoint-day"].joules == pytest.approx(20 * 0.09 * KWH_J)
    assert rs["ac-setpoint-season"].joules == pytest.approx(
        rs["ac-setpoint-day"].joules * 90)


def test_tyre_penalty_is_the_extra_fuel_only(refs):
    rs, _ = refs
    # 9 psi x 0.2%/psi = 1.8% of a year's fuel at 35 mpg
    assert rs["tyres-year"].joules == pytest.approx(
        13500 / 35 * 0.018 * 120 * MJ_J)
    # and the per-commute version is the same fraction of one day's fuel
    assert rs["tyres-commute"].joules == pytest.approx(
        rs["tyres-year"].joules * (30 / 13500))


def test_lights_and_tv_are_watts_times_hours(refs):
    rs, _ = refs
    assert rs["lights-led"].joules == pytest.approx(20 * 9 * 12 * 3600)
    assert rs["lights-incandescent"].joules == pytest.approx(20 * 60 * 12 * 3600)
    assert rs["tv-65"].joules == pytest.approx(100 * 6 * 3600)


def test_the_led_retrofit_is_the_expected_factor(refs):
    rs, _ = refs
    assert (rs["lights-incandescent"].joules / rs["lights-led"].joules
            == pytest.approx(60 / 9))


# --- the distinctions the module exists to preserve ---------------------------

def test_vehicles_are_fuel_and_household_items_are_electricity(refs):
    rs, _ = refs
    for i in ("commute-pickup", "commute-sedan", "commute-hybrid",
              "tyres-year", "tyres-commute"):
        assert rs[i].basis == FUEL, i
    for i in ("ac-setpoint-day", "lights-led", "tv-65"):
        assert rs[i].basis == ELECTRICITY, i


def test_waste_is_labelled_separately_from_consumption(refs):
    rs, _ = refs
    # you can stop setting the AC too low; you cannot as easily stop commuting
    for i in ("ac-setpoint-day", "ac-setpoint-season", "tyres-year",
              "tyres-commute", "lights-led"):
        assert rs[i].kind == AVOIDABLE, i
    for i in ("commute-pickup", "commute-sedan", "tv-65"):
        assert rs[i].kind == ABSOLUTE, i


def test_primary_energy_scales_electricity_and_leaves_fuel_alone(refs):
    rs, c = refs
    factor = c["primary_energy_factor"]
    tv = rs["tv-65"]
    assert tv.primary_joules(factor) == pytest.approx(tv.joules * factor)
    truck = rs["commute-pickup"]
    assert truck.primary_joules(factor) == pytest.approx(truck.joules)


def test_primary_basis_changes_the_ranking_against_a_run(refs):
    """The basis is not cosmetic: it moves electricity relative to fuel."""
    rs, c = refs
    rlist = list(rs.values())
    site = {r.id: f for r, f in compare(1e6, rlist, c, primary=False)}
    prim = {r.id: f for r, f in compare(1e6, rlist, c, primary=True)}
    # a fuel reference gets relatively cheaper once the run is scaled up too
    assert prim["commute-hybrid"] > site["commute-hybrid"]
    # an electricity reference is scaled on both sides, so it is unchanged
    assert prim["tv-65"] == pytest.approx(site["tv-65"])


# --- presentation -------------------------------------------------------------

def test_comparison_is_ordered_smallest_fraction_first(refs):
    rs, c = refs
    fracs = [f for _, f in compare(62928.0, list(rs.values()), c)]
    assert fracs == sorted(fracs)


def test_small_fractions_read_as_one_over_n():
    assert format_multiple(1 / 34) == "1/34"
    assert format_multiple(0.5) == "1/2"
    assert format_multiple(2.0) == "2x"
    assert format_multiple(0) == "0"


def test_every_reference_carries_a_derivation(refs):
    rs, _ = refs
    for r in rs.values():
        assert r.detail, f"{r.id} has no stated derivation"
        assert r.joules > 0


# --- the assumptions are editable, which is the point of the file -------------

def test_changing_an_assumption_changes_the_reference(tmp_path):
    data = yaml.safe_load(SPEC.read_text())
    data["commute"]["vehicles"]["pickup"]["mpg"] = 10   # a much thirstier truck
    p = tmp_path / "refs.yaml"
    p.write_text(yaml.safe_dump(data))
    rs = {r.id: r for r in load_references(p)[0]}
    assert rs["commute-pickup"].joules == pytest.approx(30 / 10 * 120 * MJ_J)


def test_a_longer_commute_scales_every_vehicle(tmp_path):
    data = yaml.safe_load(SPEC.read_text())
    data["commute"]["minutes_each_way"] = 60
    p = tmp_path / "refs.yaml"
    p.write_text(yaml.safe_dump(data))
    doubled = {r.id: r for r in load_references(p)[0]}
    base = {r.id: r for r in load_references(SPEC)[0]}
    for i in ("commute-pickup", "commute-sedan", "commute-hybrid"):
        assert doubled[i].joules == pytest.approx(base[i].joules * 2), i


# --- individual choices, industry and countries -------------------------------

from llm_energy.references import (INDIVIDUAL, INDUSTRY, NATIONAL,  # noqa: E402
                                   format_count, project_annual)


def test_kettle_is_the_heat_capacity_of_the_water(refs):
    rs, _ = refs
    # 1 L through 80 C at 85% efficient
    assert rs["kettle"].joules == pytest.approx(4186 * 1.0 * 80 / 0.85)


def test_shower_scales_with_flow_and_temperature_rise(refs):
    rs, _ = refs
    assert rs["shower"].joules == pytest.approx(4186 * (10 * 9.5) * 28)


def test_a_year_of_phone_charging_is_365_nights(refs):
    rs, _ = refs
    assert rs["phone-year"].joules == pytest.approx(rs["phone-night"].joules * 365)


def test_the_flight_counts_both_directions(refs):
    rs, _ = refs
    litres = 5550 * 2 * 3.5 / 100
    assert rs["flight-transatlantic"].joules == pytest.approx(litres * 34.7 * MJ_J)
    assert rs["flight-transatlantic"].basis == FUEL


def test_laundry_and_dryer_are_avoidable_but_a_shower_is_not(refs):
    rs, _ = refs
    assert rs["laundry-hot"].kind == AVOIDABLE
    assert rs["tumble-dryer"].kind == AVOIDABLE
    assert rs["shower"].kind == ABSOLUTE


def test_industry_references_are_power_times_time(refs):
    rs, _ = refs
    assert rs["lhc-hour"].joules == pytest.approx(200e6 * 3600)
    assert rs["hpc-cluster-day"].joules == pytest.approx(1e6 * 24 * 3600)
    assert rs["dc-rack-day"].joules == pytest.approx(10e3 * 24 * 3600)


def test_country_year_and_day_are_consistent(refs):
    rs, _ = refs
    assert rs["malta-year"].joules == pytest.approx(2.7e12 * 3600)
    assert rs["malta-day"].joules == pytest.approx(rs["malta-year"].joules / 365)


def test_each_reference_declares_its_scale(refs):
    rs, _ = refs
    assert rs["kettle"].scale == INDIVIDUAL
    assert rs["lhc-hour"].scale == INDUSTRY
    assert rs["malta-year"].scale == NATIONAL


def test_the_scales_really_are_orders_apart(refs):
    """If they were not, the runs-to-equal framing would be unnecessary."""
    rs, _ = refs
    assert rs["lhc-hour"].joules / rs["kettle"].joules > 1e6
    assert rs["malta-year"].joules / rs["lhc-hour"].joules > 1e3


# --- projecting to an aggregate ----------------------------------------------

def test_projection_multiplies_rate_actors_and_days():
    assert project_annual(1e6, runs_per_day=5, actors=1000) == pytest.approx(
        1e6 * 5 * 1000 * 365)


def test_a_projection_brings_a_country_into_range(refs):
    """One run against Malta is meaningless; a research programme is not."""
    rs, _ = refs
    one_run = 2e6
    assert one_run / rs["malta-year"].joules < 1e-8      # unreadable
    at_scale = project_annual(one_run, runs_per_day=5, actors=1000)
    assert 1e-5 < at_scale / rs["malta-year"].joules < 1e-2   # a real fraction


def test_huge_ratios_are_worded_not_written_out():
    assert format_multiple(1 / 4.86e9) == "1/4.86 billion"
    assert format_count(35.1e9) == "35.1 billion"
    assert format_count(154_000) == "154 thousand"
    assert format_count(42) == "42"
