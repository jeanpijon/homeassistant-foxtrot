"""The weekly program: the JSON file of the PLC, how the PLC evaluates it, and the schedule helper blocks."""
from __future__ import annotations

from pathlib import Path

import pytest

from custom_components.foxtrot_teco.schedule import (
    DAY, WEEKDAYS, ScheduleError, TimeProgram, helper_from_week, week_from_helper,
)

FIXTURES = Path(__file__).parent / "fixtures" / "schedules"
# the five files the PLC wrote itself; the sixth (Sauna) was uploaded by the app in its compact form
PLC_WRITTEN = ["WTP_7E62", "WTP_D03A", "WTP_1B25", "WTP_1567", "WTP_79E3"]
ALL = PLC_WRITTEN + ["WTP_A243"]
H = 3600


def load(name: str) -> TimeProgram:
    return TimeProgram.loads((FIXTURES / f"{name}.JSN").read_text(encoding="cp1250"))


@pytest.mark.parametrize("name", PLC_WRITTEN)
def test_a_file_the_plc_wrote_is_written_back_byte_for_byte(name):
    text = (FIXTURES / f"{name}.JSN").read_text(encoding="cp1250")
    assert TimeProgram.loads(text).dumps() == text


def test_the_compact_file_of_the_app_is_read_and_rewritten_in_full():
    sauna = load("WTP_A243")
    assert sauna.name == "Sauna" and sauna.heat_temp == (25.0, 21.5, 22.5, 24.5)
    assert all(len(row) == 8 for row in sauna.values)                 # the file had six per day
    assert TimeProgram.loads(sauna.dumps()) == sauna


def test_the_plc_counts_the_marks_that_have_passed():
    # Ensuit, Monday: marks 00:00 03:15 03:15 17:00 19:30, modes eco(unused) sleep comfort sleep comfort sleep
    ensuit = load("WTP_D03A")
    assert ensuit.week()[0] == [(0, "sleep"), (17 * H, "comfort"), (19 * H + 1800, "sleep")]


def test_marks_out_of_order_cannot_be_reached():
    # Pokoj, Monday: marks 00:00 06:15 21:15 15:00 21:00 and modes eco eco comfort eco limit limit.
    # The PLC stops at the first mark that is still ahead, so 15:00 and 21:00 only count once 21:15 has
    # passed: the intervals of eco (k=3) and limit (k=4) are empty and what follows 21:15 is limit.
    pokoj = load("WTP_1B25")
    assert pokoj.week()[0] == [(0, "eco"), (6 * H + 900, "comfort"), (21 * H + 900, "limit")]
    # marks beyond numTimes (here the 24:00 ones) are not used
    assert all(start < DAY for day in pokoj.week() for start, _ in day)


@pytest.mark.parametrize("name, mode", [   # what the PLC reported for each room on Monday 2026-11-16 at 23:4x
    ("WTP_7E62", "limit"), ("WTP_D03A", "sleep"), ("WTP_1B25", "limit"),
    ("WTP_1567", "comfort"), ("WTP_79E3", "sleep"), ("WTP_A243", "sleep"),
])
def test_the_week_agrees_with_what_the_plc_reported_live(name, mode):
    assert load(name).week()[0][-1][1] == mode


def test_a_day_of_one_mode_is_one_interval():
    assert load("WTP_1567").week()[0] == [(0, "comfort")]            # Obyvak: comfort all day


@pytest.mark.parametrize("name", ALL)
def test_the_week_survives_being_written_out_and_read_again(name):
    program = load(name)
    again = program.with_week(program.week())
    assert again.week() == program.week()
    assert TimeProgram.loads(again.dumps()) == again
    assert (again.heat_temp, again.cool_temp, again.holiday) == (program.heat_temp, program.cool_temp, program.holiday)


def test_a_new_week_gets_minimal_marks_that_the_plc_evaluates_as_planned():
    program = load("WTP_1B25")
    week = [[(0, "eco"), (6 * H, "comfort"), (8 * H, "eco"), (17 * H, "comfort"), (22 * H, "sleep")]] * 5 \
        + [[(0, "limit")]] * 2
    new = program.with_week(week)
    assert new.times[0] == (0, 6 * H, 8 * H, 17 * H, 22 * H, DAY, DAY)
    assert new.values[0] == (1, 1, 3, 1, 3, 2, 0, 0)                  # values[0] repeats the first mode
    assert new.times[6] == (0, DAY, DAY, DAY, DAY, DAY, DAY)
    assert new.values[6][:2] == (0, 0)
    assert new.week() == week


def test_more_intervals_than_the_thermostat_holds_are_refused():
    program = load("WTP_1B25")
    too_many = [[(0, "eco"), (H, "comfort"), (2 * H, "eco"), (3 * H, "comfort"), (4 * H, "eco"), (5 * H, "comfort")]] * 7
    with pytest.raises(ScheduleError, match="monday needs 6 intervals"):
        program.with_week(too_many)


def block(start, end, mode=None, **data):
    return {"from": start, "to": end, **({"data": {"mode": mode, **data}} if mode else {})}


def week_of(**days):
    return {day: days.get(day, []) for day in WEEKDAYS}


def test_gaps_in_the_helper_take_the_base_mode():
    schedule = week_of(monday=[block("06:00:00", "08:00:00", "comfort"), block("17:00:00", "24:00:00", "comfort")])
    week = week_from_helper(schedule, "eco")
    assert week[0] == [(0, "eco"), (6 * H, "comfort"), (8 * H, "eco"), (17 * H, "comfort")]
    assert week[1] == [(0, "eco")]


def test_blocks_of_the_same_mode_that_touch_become_one_interval():
    schedule = week_of(monday=[block("06:00:00", "08:00:00", "comfort"), block("08:00:00", "09:00:00", "comfort"),
                               block("09:00:00", "23:59:59", "sleep")])
    assert week_from_helper(schedule, "eco")[0] == [(0, "eco"), (6 * H, "comfort"), (9 * H, "sleep")]


def test_a_block_may_end_at_midnight_either_way():
    for end in ("24:00:00", "23:59:59", "23:59:59.999999"):
        week = week_from_helper(week_of(tuesday=[block("20:00:00", end, "sleep")]), "eco")
        assert week[1] == [(0, "eco"), (20 * H, "sleep")]


@pytest.mark.parametrize("schedule, message", [
    (week_of(monday=[block("06:00:00", "08:00:00")]), "needs data mode"),
    (week_of(monday=[block("06:00:00", "08:00:00", "boost")]), "needs data mode"),
    (week_of(monday=[block("06:00:00", "09:00:00", "comfort"), block("08:00:00", "10:00:00", "eco")]), "overlap"),
    (week_of(friday=[block("six", "08:00:00", "comfort")]), "not a time of day"),
])
def test_a_helper_that_cannot_be_a_program_is_explained(schedule, message):
    with pytest.raises(ScheduleError, match=message):
        week_from_helper(schedule, "eco")


@pytest.mark.parametrize("name", ALL)
def test_the_helper_blocks_of_a_program_make_the_same_week_again(name):
    week = load(name).week()
    for base in ("eco", "sleep", "limit"):
        assert week_from_helper(helper_from_week(week, base), base) == week


def test_only_intervals_that_differ_from_the_base_become_blocks():
    blocks = helper_from_week(load("WTP_D03A").week(), "sleep")
    assert blocks["monday"] == [
        {"from": "17:00:00", "to": "19:30:00", "data": {"mode": "comfort"}},
    ]
