# constraint-shift-scheduler

A monthly staff-shift scheduler built on [Google OR-Tools CP-SAT](https://developers.google.com/optimization/cp/cp_solver).
The core is a single **pure function** — feed it staff, requests and a required-headcount
curve, and it returns a legal, optimized month-long roster. No database, no I/O, no global
state, which makes it easy to test, embed, and reason about.

This is an extracted, fully generalized core of a shift-scheduling system the author runs
in production. All data in this repo is synthetic.

## Why this is interesting

Staff scheduling is a deceptively hard combinatorial problem: dozens of people, 30-minute
granularity, labour-law limits, individual day/time availability, days-off requests, target
labour cost — most of which *conflict*. A greedy or template approach breaks the moment two
rules disagree. Modeling it as a **constraint optimization problem** lets you state every
rule once (as a hard constraint or a weighted soft penalty) and let the solver find the best
feasible trade-off.

## The model

| Element | Encoding |
|---|---|
| Decision variable | `assigned[staff, day, slot]` — boolean, on duty in a 30-min slot |
| Day granularity | 48 slots/day (`slot 0 = 00:00`, `slot 47 = 23:30`) |
| One block per day | at most one `off→on` transition per (staff, day) ⇒ a single contiguous shift |
| Min / max shift | ≥ 3h (or zero) and ≤ 8h per day |
| Weekly cap | rolling 7-day window ≤ 40h |
| Consecutive days | ≤ 6 working days in a row |
| Minor protection | no 22:00–05:00 slots for `is_minor` staff (**hard**) |
| Days-off requests | **hard** |
| Work requests / time ranges | **soft**, high penalty |
| Availability window | **hard**; preferred shift range **soft** |
| Required headcount | `Σ assigned + shortage − excess == required` (slack penalized) |
| Labour-cost target & per-staff min income | **soft** |
| History continuity | **soft**, weighted by generation mode |

The objective is a weighted sum of all soft-penalty terms, minimized.

### Generation modes

How strongly historical patterns steer the result:

| Mode | Behavior |
|---|---|
| `prompt-first` | ignore history entirely |
| `balanced` | history is a weak hint |
| `veteran-first` | apply history only to a supplied set of veteran staff |
| `pattern-strong` *(default)* | history is a strong guide |

### Manager weights

Each of `shortage`, `pref_slot`, `cost`, `min_salary` accepts an integer `1..5`
(`3` = default) that scales its penalty `0.2× / 0.5× / 1.0× / 3.0× / 10.0×`, so a
non-technical manager can re-balance "fill every slot" vs. "honor everyone's preferred hours"
without touching code.

## Quickstart

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python examples/demo.py pattern-strong
```

```python
from shift_scheduler import solve_shift

assignments, stats = solve_shift(
    staff=[
        {'name': 'Alice', 'sid': 's1', 'is_minor': False, 'wage': 1200},
        {'name': 'Eve',   'sid': 's5', 'is_minor': True,  'wage': 1050},
    ],
    requests={'s5': {3: {'type': 'off'}}},          # Eve wants day 3 off (hard)
    required_per_day=[[2] * 48 for _ in range(7)],  # 2 people every slot, 7 days
    past_pattern={},
    days_in_month=7, year=2026, month=5,
    time_limit_sec=10,
)
# assignments -> [{'name': 'Alice', 'days': {'1': '8-16', '2': '', ...}}, ...]
# stats       -> {'status': 'FEASIBLE', 'shortage_total': ..., 'labor_cost_total': ...}
```

## Sample output

```
Solving a 7-day roster for 5 staff (mode=pattern-strong)...

status   : FEASIBLE
shortage : 146 slot-units  excess: 0 slot-units

name           d1       d2       d3       d4       d5       d6       d7
-----------------------------------------------------------------------
Alice        8-16        ·    10-18        ·     8-1612.5-20.5    10-18
Bob      4.5-12.5     6-13     6-10     6-14        ·   6-12.5   6.5-13
Carol     16.5-23  15.5-23  15.5-23    16-23  16.5-23    18-23        ·
Dave            · 7.5-15.5     7-15     7-15     5-13     7-15        ·
Eve         14-22    13-21        ·    14-22    13-21        ·    13-21
```

Note in this run: **Bob's day-5 off request is honored** (empty), **Carol's day-3
20:00–23:00 work request is covered** (`15.5-23`), and **Eve (a minor) never gets a
22:00–05:00 slot**.

## Tests

```bash
pip install pytest
pytest tests/ -q
```

The suite asserts the hard guarantees that must never break — days-off honored, work-request
ranges covered, minor night-ban respected, no sub-3h shift, and feasibility across all four
modes.

## Using it on real data

`solve_shift` is intentionally I/O-free. To deploy it, write a thin adapter that:

1. pulls your staff list, requests, required-headcount curve and (optionally) historical
   shift data from your store of record,
2. shapes them into the dictionaries above,
3. calls `solve_shift`, and
4. writes `assignments` back.

The author's production deployment runs exactly this split: a Firestore/Cloud Run adapter
around an identical solver core.

## License

MIT — see [LICENSE](LICENSE).
