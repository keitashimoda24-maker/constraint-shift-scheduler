"""constraint-shift-scheduler: a pure CP-SAT monthly staff shift solver."""

from .solver import (
    solve_shift,
    format_slot_range,
    hhmm_to_slot,
    parse_shift_str,
    SLOTS_PER_DAY,
    MIN_SHIFT_SLOTS,
    MAX_DAILY_SLOTS,
    MAX_WEEKLY_SLOTS,
    MAX_CONSECUTIVE_DAYS,
    MINOR_FORBIDDEN_SLOTS,
)

__all__ = [
    'solve_shift',
    'format_slot_range',
    'hhmm_to_slot',
    'parse_shift_str',
    'SLOTS_PER_DAY',
    'MIN_SHIFT_SLOTS',
    'MAX_DAILY_SLOTS',
    'MAX_WEEKLY_SLOTS',
    'MAX_CONSECUTIVE_DAYS',
    'MINOR_FORBIDDEN_SLOTS',
]

__version__ = '0.1.0'
