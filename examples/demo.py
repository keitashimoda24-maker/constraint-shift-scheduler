"""Run the CP-SAT scheduler on the synthetic sample and print the roster.

    python examples/demo.py [mode]

mode is one of: prompt-first | balanced | veteran-first | pattern-strong
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shift_scheduler import solve_shift  # noqa: E402
from synthetic_data import sample_case   # noqa: E402


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else 'pattern-strong'
    case = sample_case(days_in_month=7)
    veteran_sids = {'s1', 's2', 's3'} if mode == 'veteran-first' else None

    print(f'Solving a {case["days_in_month"]}-day roster for '
          f'{len(case["staff"])} staff (mode={mode})...\n')

    assignments, stats = solve_shift(
        **case, mode=mode, veteran_sids=veteran_sids, time_limit_sec=20
    )

    print(f'status   : {stats["status"]}')
    print(f'objective: {stats["objective"]}')
    print(f'wall time: {stats["wall_time_sec"]:.2f}s')
    print(f'shortage : {stats.get("shortage_total")} slot-units  '
          f'excess: {stats.get("excess_total")} slot-units')
    if stats.get('request_warnings'):
        print('warnings :', stats['request_warnings'])
    print()

    days = case['days_in_month']
    header = 'name '.ljust(8) + ''.join(f'd{d}'.rjust(9) for d in range(1, days + 1))
    print(header)
    print('-' * len(header))
    for a in assignments:
        row = a['name'].ljust(8)
        for d in range(1, days + 1):
            row += (a['days'].get(str(d)) or '·').rjust(9)
        print(row)


if __name__ == '__main__':
    main()
