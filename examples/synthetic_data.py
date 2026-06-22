"""Synthetic (fully fictional) inputs for the CP-SAT shift scheduler.

Nothing here comes from a real organization — names are placeholders and the
numbers are illustrative. Use this as a template for wiring up your own data.
"""

from shift_scheduler import SLOTS_PER_DAY


# Five fictional staff. ``is_minor=True`` activates the 22:00-05:00 night ban.
# ``wage`` is per hour, in whatever integer currency unit you like.
SAMPLE_STAFF = [
    {'name': 'Alice', 'sid': 's1', 'is_minor': False, 'wage': 1200},
    {'name': 'Bob',   'sid': 's2', 'is_minor': False, 'wage': 1300},
    {'name': 'Carol', 'sid': 's3', 'is_minor': False, 'wage': 1100},
    {'name': 'Dave',  'sid': 's4', 'is_minor': False, 'wage': 1500},
    {'name': 'Eve',   'sid': 's5', 'is_minor': True,  'wage': 1050},  # minor
]


def make_required(days, light=1, busy=2):
    """Build a required-headcount curve repeated across ``days``.

    Default profile: 1 person 00:00-06:00, 2 people 06:00-22:00,
    1 person 22:00-23:00, nobody 23:00-24:00.
    """
    arr = [0] * SLOTS_PER_DAY
    for t in range(0, 12):    # 00:00-06:00
        arr[t] = light
    for t in range(12, 44):   # 06:00-22:00
        arr[t] = busy
    for t in range(44, 46):   # 22:00-23:00
        arr[t] = light
    return [arr[:] for _ in range(days)]


# Requests: Carol wants to work day 3, 20:00-23:00 (slots 40-46);
# Bob wants day 5 off.
SAMPLE_REQUESTS = {
    's3': {3: {'type': 'work', 'start_slot': 40, 'end_slot': 46}},
    's2': {5: {'type': 'off'}},
}


# Historical pattern per staff per day-of-week (0=Sun .. 6=Sat).
# works_prob: how often they historically worked; avg_start/avg_end: typical slots.
SAMPLE_PAST_PATTERN = {
    's1': {dow: {'works_prob': 0.6, 'avg_start': 16, 'avg_end': 36} for dow in range(7)},
    's2': {dow: {'works_prob': 0.7, 'avg_start': 12, 'avg_end': 30} for dow in range(7)},
    's3': {dow: {'works_prob': 0.5, 'avg_start': 30, 'avg_end': 46} for dow in range(7)},
    's4': {dow: {'works_prob': 0.7, 'avg_start': 14, 'avg_end': 32} for dow in range(7)},
    's5': {dow: {'works_prob': 0.4, 'avg_start': 26, 'avg_end': 42} for dow in range(7)},
}


def sample_case(days_in_month=7):
    """Return a ready-to-solve kwargs dict for ``solve_shift``."""
    return dict(
        staff=SAMPLE_STAFF,
        requests=SAMPLE_REQUESTS,
        required_per_day=make_required(days_in_month),
        past_pattern=SAMPLE_PAST_PATTERN,
        days_in_month=days_in_month,
        year=2026,
        month=5,
    )
