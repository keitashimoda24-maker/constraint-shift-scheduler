"""Constraint tests for solve_shift — pure function, fully synthetic data.

These verify the *hard* guarantees the solver must never break:
    - days-off requests are honored
    - work requests with a time range land in that range
    - minors never get 22:00-05:00 slots
    - no shift is shorter than the 3h minimum
    - all four generation modes return a feasible schedule
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'examples'))

from shift_scheduler import solve_shift  # noqa: E402
from synthetic_data import sample_case   # noqa: E402


def _solve(mode='pattern-strong', veteran_sids=None, days=7):
    case = sample_case(days_in_month=days)
    # A short time limit is enough to reach a feasible solution that satisfies
    # every hard constraint; we assert on feasibility, not optimality.
    return solve_shift(**case, mode=mode, veteran_sids=veteran_sids, time_limit_sec=8)


def _slots(val):
    """'8-16' -> (16, 32) slot pair; '' -> None."""
    if not val:
        return None
    ss, ee = (float(x) for x in val.split('-'))
    return int(round(ss * 2)), int(round(ee * 2))


def test_feasible():
    _, stats = _solve()
    assert stats['status'] in ('OPTIMAL', 'FEASIBLE')


def test_off_request_is_hard():
    # Bob (s2) requested day 5 off — must be empty.
    assignments, _ = _solve()
    bob = next(a for a in assignments if a['name'] == 'Bob')
    assert bob['days'].get('5', '') == ''


def test_work_request_range_is_covered():
    # Carol (s3) requested day 3, 20:00-23:00 (slots 40-46).
    assignments, _ = _solve()
    carol = next(a for a in assignments if a['name'] == 'Carol')
    rng = _slots(carol['days'].get('3'))
    assert rng is not None, 'Carol day 3 should not be empty'
    ss, ee = rng
    assert ss <= 40 and ee >= 46, f'Carol day 3 {carol["days"]["3"]} must cover 20-23'


def test_minor_never_works_night():
    # Eve (s5) is a minor: no slot before 05:00 (10) or at/after 22:00 (44).
    assignments, _ = _solve()
    eve = next(a for a in assignments if a['name'] == 'Eve')
    for dkey, val in eve['days'].items():
        rng = _slots(val)
        if rng is None:
            continue
        ss, ee = rng
        assert ss >= 10 and ee <= 44, f'minor day {dkey}={val} violates 22:00-05:00 ban'


def test_no_shift_shorter_than_3h():
    assignments, _ = _solve()
    for a in assignments:
        for dkey, val in a['days'].items():
            rng = _slots(val)
            if rng is None:
                continue
            ss, ee = rng
            assert (ee - ss) >= 6, f'{a["name"]} day {dkey}={val} is shorter than 3h'


@pytest.mark.parametrize('mode,vets', [
    ('prompt-first', None),
    ('balanced', None),
    ('veteran-first', {'s1', 's2', 's3'}),
    ('pattern-strong', None),
])
def test_all_modes_feasible(mode, vets):
    assignments, stats = _solve(mode=mode, veteran_sids=vets)
    assert stats['status'] in ('OPTIMAL', 'FEASIBLE')
    # Hard guarantees hold regardless of mode:
    bob = next(a for a in assignments if a['name'] == 'Bob')
    assert bob['days'].get('5', '') == ''
    eve = next(a for a in assignments if a['name'] == 'Eve')
    for val in eve['days'].values():
        rng = _slots(val)
        if rng:
            assert rng[0] >= 10 and rng[1] <= 44
