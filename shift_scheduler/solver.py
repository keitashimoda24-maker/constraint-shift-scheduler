"""
CP-SAT shift scheduler — a pure-function constraint solver for monthly staff rosters.

Built on Google OR-Tools CP-SAT. `solve_shift(...)` is a pure function: feed it
staff, requests, required-staffing and (optional) historical patterns, and it
returns a month-long schedule. No I/O, no database, no global state — which makes
it trivial to test and embed.

Model summary
-------------
    Decision var : assigned[s, d, t]  BoolVar  (staff s on duty on day d, slot t)
    Auxiliary    : works[s, d] BoolVar, duration[s, d] IntVar
    Contiguity   : at most one off->on transition per (s, d)  => one block/day
    Min shift    : duration >= 6 slots (3h) OR duration == 0
    Required     : sum_s assigned[s,d,t] + shortage - excess == required[d,t]
    Soft terms   : work-request honoring, preferred-slot adherence, historical
                   pattern continuity, labour-cost target, per-staff min income

Slot model: 30-minute granularity, 48 slots/day. slot 0 = 00:00, slot 47 = 23:30.

Generation modes (how strongly past patterns steer the result):
    prompt-first   : ignore history entirely
    balanced       : history is a weak hint
    veteran-first  : apply history only to a given set of veteran staff
    pattern-strong : history is a strong guide (default)
"""

from __future__ import annotations

from datetime import date


SLOTS_PER_DAY = 48
MIN_SHIFT_SLOTS = 6              # 3 hours
MAX_DAILY_SLOTS = 16            # 8 hours
MAX_WEEKLY_SLOTS = 80          # 40 hours
MAX_CONSECUTIVE_DAYS = 6
# Night work 22:00-05:00 is forbidden for minors -> slots [44,48) and [0,10)
MINOR_FORBIDDEN_SLOTS = frozenset(list(range(0, 10)) + list(range(44, 48)))


def solve_shift(
    staff,                  # [{'name': str, 'sid': str, 'is_minor': bool, 'wage': int}]
    requests,               # {sid: {day(int): {'type': 'off'|'work', 'start_slot': int|None, 'end_slot': int|None}}}
    required_per_day,       # list[days_in_month] of list[SLOTS_PER_DAY] of int
    past_pattern,           # {sid: {dow(0..6): {'works_prob': float, 'avg_start': int, 'avg_end': int}}}
    days_in_month,
    year,
    month,
    time_limit_sec=30,
    num_workers=4,
    target_labor_cost=0,    # currency/month. 0 disables.
    min_labor_cost=0,       # currency/month. 0 disables.
    staff_prefs=None,       # {sid: {'days_set': set[int]|None, 'sched': {dow: {'avail_s','avail_e','pref_s','pref_e'}}, 'min_salary': int}}
    mode='pattern-strong',  # 'prompt-first' | 'balanced' | 'veteran-first' | 'pattern-strong'
    veteran_sids=None,      # set[str] — used in 'veteran-first' mode
    weights=None,           # {'shortage','pref_slot','cost','min_salary'} each 1..5 (3 = default)
    allow_under_staff=False,  # True: drop the staffing-shortage penalty so honoring
                              # requests beats filling every slot.
):
    """Solve a one-month shift schedule using CP-SAT.

    The day-of-week index ``dow`` follows the convention 0=Sunday .. 6=Saturday.

    Returns: (assignments, stats)
        assignments: [{'name': str, 'days': {'1': '8-16', '2': '', ...}}]
        stats: dict with status / objective / wall_time / shortage / excess
    """
    try:
        from ortools.sat.python import cp_model
    except ImportError as e:
        raise RuntimeError(f'ortools is required for the CP-SAT solver: {e}')

    model = cp_model.CpModel()
    n_staff = len(staff)
    S = list(range(n_staff))
    days = list(range(1, days_in_month + 1))

    # ---------- Variables ----------
    assigned = {}
    works = {}
    duration = {}
    for s_idx in S:
        for d in days:
            works[s_idx, d] = model.NewBoolVar(f'w_{s_idx}_{d}')
            duration[s_idx, d] = model.NewIntVar(0, MAX_DAILY_SLOTS, f'dur_{s_idx}_{d}')
            for t in range(SLOTS_PER_DAY):
                assigned[s_idx, d, t] = model.NewBoolVar(f'a_{s_idx}_{d}_{t}')

    # ---------- Link works <-> assigned, enforce min/max shift length ----------
    for s_idx in S:
        for d in days:
            day_sum = sum(assigned[s_idx, d, t] for t in range(SLOTS_PER_DAY))
            model.Add(duration[s_idx, d] == day_sum)
            model.Add(duration[s_idx, d] >= MIN_SHIFT_SLOTS).OnlyEnforceIf(works[s_idx, d])
            model.Add(duration[s_idx, d] == 0).OnlyEnforceIf(works[s_idx, d].Not())
            # Daily cap (max 8h) is enforced by the duration variable's domain.

    # ---------- Contiguity: at most one off->on transition per (s, d) ----------
    # Combined with works = OR(assigned), this forces a single contiguous block/day.
    for s_idx in S:
        for d in days:
            transitions = [assigned[s_idx, d, 0]]  # slot 0 starting "on" counts as a start
            for t in range(1, SLOTS_PER_DAY):
                tr = model.NewBoolVar(f'tr_{s_idx}_{d}_{t}')
                # tr <=> (assigned[t] == 1 AND assigned[t-1] == 0)
                model.AddBoolAnd(
                    [assigned[s_idx, d, t], assigned[s_idx, d, t - 1].Not()]
                ).OnlyEnforceIf(tr)
                model.AddBoolOr(
                    [assigned[s_idx, d, t].Not(), assigned[s_idx, d, t - 1]]
                ).OnlyEnforceIf(tr.Not())
                transitions.append(tr)
            model.Add(sum(transitions) <= 1)

    # ---------- Max consecutive working days ----------
    for s_idx in S:
        for start_d in range(1, days_in_month - MAX_CONSECUTIVE_DAYS + 1):
            window = [works[s_idx, start_d + i] for i in range(MAX_CONSECUTIVE_DAYS + 1)]
            model.Add(sum(window) <= MAX_CONSECUTIVE_DAYS)

    # ---------- Rolling 7-day weekly hours <= 40h ----------
    for s_idx in S:
        for start_d in range(1, days_in_month - 6 + 1):
            window_dur = [duration[s_idx, start_d + i] for i in range(7)]
            model.Add(sum(window_dur) <= MAX_WEEKLY_SLOTS)

    # ---------- Requests (off / work) ----------
    # off  : hard constraint (guaranteed days off / legal compliance)
    # work : soft constraint (honor a work request at high penalty, but yield to
    #        hard rules such as the max-consecutive-days cap when they conflict)
    sid_to_idx = {staff[i]['sid']: i for i in S if staff[i].get('sid')}
    request_warnings = []
    work_request_miss_vars = []  # added to the objective later
    work_slot_miss_vars = []     # slot-range deviation penalties
    # (sid, d) pairs where a work-request specifies a time range; preferred-shift
    # soft penalties are suppressed for these days.
    work_request_day_set = set()
    for sid, day_reqs in (requests or {}).items():
        if sid not in sid_to_idx:
            continue
        s_idx = sid_to_idx[sid]
        s_name = staff[s_idx].get('name', sid)
        s_minor = bool(staff[s_idx].get('is_minor'))
        for d, req in (day_reqs or {}).items():
            try:
                d = int(d)
            except (TypeError, ValueError):
                continue
            if d < 1 or d > days_in_month:
                continue
            rtype = (req or {}).get('type')
            if rtype == 'off':
                model.Add(works[s_idx, d] == 0)
            elif rtype == 'work':
                # Soft: penalize works[s, d] == 0 (failing to honor a work request)
                wmiss = model.NewBoolVar(f'wmiss_{s_idx}_{d}')
                model.Add(works[s_idx, d] == 0).OnlyEnforceIf(wmiss)
                model.Add(works[s_idx, d] == 1).OnlyEnforceIf(wmiss.Not())
                work_request_miss_vars.append(wmiss)

                ss = req.get('start_slot')
                ee = req.get('end_slot')
                if ss is not None and ee is not None and ee > ss:
                    ss = max(0, int(ss))
                    ee = min(SLOTS_PER_DAY, int(ee))
                    span = ee - ss
                    if span > MAX_DAILY_SLOTS:
                        new_ee = ss + MAX_DAILY_SLOTS
                        request_warnings.append(
                            f'{s_name} d{d}: work request {span/2}h trimmed to '
                            f'{MAX_DAILY_SLOTS/2}h ({ss/2}-{ee/2} -> {ss/2}-{new_ee/2})'
                        )
                        ee = new_ee
                    if s_minor:
                        overlap = [t for t in range(ss, ee) if t in MINOR_FORBIDDEN_SLOTS]
                        if overlap:
                            request_warnings.append(
                                f'{s_name} d{d}: time range ignored (overlaps minor '
                                f'night-work ban) ({ss/2}-{ee/2})'
                            )
                            continue
                    # Soft: prefer assigned[t] == 1 within the requested range
                    for t in range(ss, ee):
                        smiss = model.NewBoolVar(f'smiss_{s_idx}_{d}_{t}')
                        # smiss == 1 iff (works == 1 AND assigned[t] == 0):
                        # on duty but not at the requested slot
                        model.AddBoolAnd([works[s_idx, d], assigned[s_idx, d, t].Not()]).OnlyEnforceIf(smiss)
                        model.AddBoolOr([works[s_idx, d].Not(), assigned[s_idx, d, t]]).OnlyEnforceIf(smiss.Not())
                        work_slot_miss_vars.append(smiss)
                    # Prefer the work-request range over the generic preferred shift on this day
                    work_request_day_set.add((s_idx, d))

    # ---------- Minor forbidden slots ----------
    for s_idx in S:
        if not staff[s_idx].get('is_minor'):
            continue
        for d in days:
            for t in MINOR_FORBIDDEN_SLOTS:
                model.Add(assigned[s_idx, d, t] == 0)

    # ---------- Staff-level prefs (available days/times = HARD, preferred shift = SOFT) ----------
    sid_to_idx_full = {staff[i].get('sid'): i for i in S if staff[i].get('sid')}
    pref_slot_miss_vars = []
    for sid, prefs in (staff_prefs or {}).items():
        if sid not in sid_to_idx_full:
            continue
        s_idx = sid_to_idx_full[sid]
        days_set = prefs.get('days_set')  # None => no restriction
        sched = prefs.get('sched') or {}
        for d in days:
            try:
                py_dow = date(year, month, d).weekday()
            except Exception:
                continue
            dow = (py_dow + 1) % 7
            # HARD: day-of-week restriction (unchecked days are fully off)
            if days_set is not None and dow not in days_set:
                model.Add(works[s_idx, d] == 0)
                continue
            sd_sched = sched.get(dow)
            if not sd_sched:
                continue
            # HARD: outside the available time window => assigned = 0
            avail_s = sd_sched.get('avail_s')
            avail_e = sd_sched.get('avail_e')
            if avail_s is not None and avail_e is not None and avail_e > avail_s:
                for t in range(SLOTS_PER_DAY):
                    if t < avail_s or t >= avail_e:
                        model.Add(assigned[s_idx, d, t] == 0)
            # SOFT: penalize working outside the preferred shift range.
            # Skip on days where an explicit work-request time range takes precedence.
            if (s_idx, d) in work_request_day_set:
                continue
            pref_s = sd_sched.get('pref_s')
            pref_e = sd_sched.get('pref_e')
            if pref_s is not None and pref_e is not None and pref_e > pref_s:
                for t in range(SLOTS_PER_DAY):
                    if pref_s <= t < pref_e:
                        continue
                    pmiss = model.NewBoolVar(f'pref_{s_idx}_{d}_{t}')
                    # pmiss == 1 iff (works == 1 AND assigned[t] == 1) outside preferred range
                    model.AddBoolAnd([works[s_idx, d], assigned[s_idx, d, t]]).OnlyEnforceIf(pmiss)
                    model.AddBoolOr([works[s_idx, d].Not(), assigned[s_idx, d, t].Not()]).OnlyEnforceIf(pmiss.Not())
                    pref_slot_miss_vars.append(pmiss)

    # ---------- Required staffing with slack ----------
    shortage = {}
    excess = {}
    max_req_t = 0
    for d in days:
        req_arr = required_per_day[d - 1] if d - 1 < len(required_per_day) else [0] * SLOTS_PER_DAY
        for t in range(SLOTS_PER_DAY):
            req_t = req_arr[t] if t < len(req_arr) else 0
            if req_t > max_req_t:
                max_req_t = req_t
    slack_ub = max(n_staff, max_req_t)
    for d in days:
        req_arr = required_per_day[d - 1] if d - 1 < len(required_per_day) else [0] * SLOTS_PER_DAY
        for t in range(SLOTS_PER_DAY):
            req_t = req_arr[t] if t < len(req_arr) else 0
            shortage[d, t] = model.NewIntVar(0, slack_ub, f'sh_{d}_{t}')
            excess[d, t] = model.NewIntVar(0, slack_ub, f'ex_{d}_{t}')
            model.Add(
                sum(assigned[s_idx, d, t] for s_idx in S)
                + shortage[d, t] - excess[d, t] == req_t
            )

    # ---------- Objective ----------
    # Manager-tunable weights (1..5 -> multiplier):
    #   1 = 0.2x / 2 = 0.5x / 3 = 1.0x (default) / 4 = 3.0x / 5 = 10.0x
    # With all weights at the default 3, behavior matches weights=None exactly.
    _WMAP = {1: 0.2, 2: 0.5, 3: 1.0, 4: 3.0, 5: 10.0}

    def _mult(key):
        try:
            return _WMAP.get(int((weights or {}).get(key, 3)), 1.0)
        except Exception:
            return 1.0

    m_shortage = _mult('shortage')
    m_pref = _mult('pref_slot')
    m_cost = _mult('cost')
    m_min_sal = _mult('min_salary')

    # Base weights (weights=None or all-3 reproduces the canonical behavior).
    # allow_under_staff=True slashes the shortage/excess penalty so the solver
    # prefers leaving a slot empty over breaking preferred/standard shift ranges.
    if allow_under_staff:
        _BASE_SHORTAGE = 100   # normally 50000 -> 100. Below W_WORK_REQUEST(5000)/W_PREF_SLOT(2000).
        _BASE_EXCESS = 100
    else:
        _BASE_SHORTAGE = 50000
        _BASE_EXCESS = 10000
    _BASE_PREF_SLOT = 2000
    _BASE_COST_OVER = 10
    _BASE_COST_UNDER = 5
    _BASE_MIN_SALARY = 1

    W_SHORTAGE = max(1, int(_BASE_SHORTAGE * m_shortage))
    W_EXCESS = max(1, int(_BASE_EXCESS * m_shortage))  # over-staffing weighted like under-staffing
    W_WORK_REQUEST = 5000     # soft: violating a work-request day (works=0 when work requested)
    W_WORK_SLOT = 30000       # soft: working but outside the requested slot range.
                              #   Set above excess(10000) + cost_over so the requested
                              #   time wins even when those penalties stack.
    # Past-pattern weights switch by mode:
    #   pattern-strong (default): 3000/500 — history is a strong guide
    #   balanced                : 300/30  — prompt-driven, history a faint hint
    #   prompt-first            : 0/0     — history disabled
    #   veteran-first           : 3000/500 — but only for veteran_sids
    if mode == 'prompt-first':
        W_PATTERN_DAY = 0
        W_PATTERN_SLOT = 0
    elif mode == 'balanced':
        W_PATTERN_DAY = 300
        W_PATTERN_SLOT = 30
    else:  # 'veteran-first' or 'pattern-strong'
        W_PATTERN_DAY = 3000
        W_PATTERN_SLOT = 500
    veteran_sids = veteran_sids or set()

    W_COST_OVER = max(1, int(_BASE_COST_OVER * m_cost))    # soft: per unit over the labour-cost target
    W_COST_UNDER = max(1, int(_BASE_COST_UNDER * m_cost))  # soft: per unit under the minimum labour cost
    W_PREF_SLOT = max(1, int(_BASE_PREF_SLOT * m_pref))    # soft: working outside preferred shift range
    W_MIN_SALARY = max(1, int(_BASE_MIN_SALARY * m_min_sal))  # soft: per unit below a staff member's target income

    obj_terms = []
    for d in days:
        for t in range(SLOTS_PER_DAY):
            obj_terms.append(shortage[d, t] * W_SHORTAGE)
            obj_terms.append(excess[d, t] * W_EXCESS)

    for v in work_request_miss_vars:
        obj_terms.append(v * W_WORK_REQUEST)
    for v in work_slot_miss_vars:
        obj_terms.append(v * W_WORK_SLOT)
    for v in pref_slot_miss_vars:
        obj_terms.append(v * W_PREF_SLOT)

    # Soft: per-day works-probability + per-slot time-of-day continuity with history.
    # mode='prompt-first' -> skip all. mode='veteran-first' -> veteran_sids only.
    _apply_pattern = (W_PATTERN_DAY > 0) or (W_PATTERN_SLOT > 0)
    for s_idx, sdata in enumerate(staff):
        if not _apply_pattern:
            break
        sid = sdata.get('sid')
        if not sid:
            continue
        if mode == 'veteran-first' and sid not in veteran_sids:
            continue
        pp = (past_pattern or {}).get(sid, {})
        if not pp:
            continue
        for d in days:
            try:
                py_dow = date(year, month, d).weekday()
            except Exception:
                continue
            dow = (py_dow + 1) % 7
            past = pp.get(dow)
            if not past:
                continue
            wp = past.get('works_prob', 0.0)
            avg_s = int(past.get('avg_start', 0) or 0)
            avg_e = int(past.get('avg_end', 0) or 0)
            # Day-level: penalize works mismatch against the historical probability
            if wp >= 0.5:
                miss = model.NewBoolVar(f'pat_off_{s_idx}_{d}')
                model.Add(works[s_idx, d] == 0).OnlyEnforceIf(miss)
                model.Add(works[s_idx, d] == 1).OnlyEnforceIf(miss.Not())
                obj_terms.append(miss * W_PATTERN_DAY)
            else:
                miss = model.NewBoolVar(f'pat_on_{s_idx}_{d}')
                model.Add(works[s_idx, d] == 1).OnlyEnforceIf(miss)
                model.Add(works[s_idx, d] == 0).OnlyEnforceIf(miss.Not())
                obj_terms.append(miss * W_PATTERN_DAY)
            # Slot-level: pull the shift toward historically common hours (only when works=1).
            if 0 <= avg_s < SLOTS_PER_DAY and avg_s < avg_e <= SLOTS_PER_DAY and wp >= 0.3:
                for t in range(SLOTS_PER_DAY):
                    past_on = (avg_s <= t < avg_e)
                    smm = model.NewBoolVar(f'psm_{s_idx}_{d}_{t}')
                    if past_on:
                        # penalize works=1 AND assigned[t]=0
                        model.AddBoolAnd([works[s_idx, d], assigned[s_idx, d, t].Not()]).OnlyEnforceIf(smm)
                        model.AddBoolOr([works[s_idx, d].Not(), assigned[s_idx, d, t]]).OnlyEnforceIf(smm.Not())
                    else:
                        # lightly penalize being on duty during historically idle hours
                        model.Add(assigned[s_idx, d, t] == 1).OnlyEnforceIf(smm)
                        model.Add(assigned[s_idx, d, t] == 0).OnlyEnforceIf(smm.Not())
                    obj_terms.append(smm * W_PATTERN_SLOT)

    # ---------- Labour-cost soft constraint ----------
    # Monthly labour cost = sum of wage_per_30min * assigned[s, d, t]
    target_labor_cost = int(target_labor_cost or 0)
    min_labor_cost = int(min_labor_cost or 0)
    if target_labor_cost > 0 or min_labor_cost > 0:
        cost_terms = []
        for s_idx, sdata in enumerate(staff):
            wage = int(sdata.get('wage') or 0)
            if wage <= 0:
                continue
            half_wage = wage // 2  # currency / 30min
            for d in days:
                for t in range(SLOTS_PER_DAY):
                    cost_terms.append(assigned[s_idx, d, t] * half_wage)
        if cost_terms:
            total_cost = model.NewIntVar(0, 100_000_000, 'total_cost')
            model.Add(total_cost == sum(cost_terms))
            if target_labor_cost > 0:
                cost_excess_var = model.NewIntVar(0, 100_000_000, 'cost_excess')
                model.Add(cost_excess_var >= total_cost - target_labor_cost)
                obj_terms.append(cost_excess_var * W_COST_OVER)
            if min_labor_cost > 0:
                cost_under_var = model.NewIntVar(0, 100_000_000, 'cost_under')
                model.Add(cost_under_var >= min_labor_cost - total_cost)
                obj_terms.append(cost_under_var * W_COST_UNDER)

    # ---------- Per-staff minimum target income (soft) ----------
    for sid, prefs in (staff_prefs or {}).items():
        if sid not in sid_to_idx_full:
            continue
        s_idx = sid_to_idx_full[sid]
        try:
            min_sal = int(prefs.get('min_salary') or 0)
        except Exception:
            min_sal = 0
        if min_sal <= 0:
            continue
        wage = int((staff[s_idx].get('wage') or 0))
        if wage <= 0:
            continue
        half_wage = wage // 2
        s_cost_terms = [
            assigned[s_idx, d, t] * half_wage
            for d in days for t in range(SLOTS_PER_DAY)
        ]
        s_cost_var = model.NewIntVar(0, 100_000_000, f's_cost_{s_idx}')
        model.Add(s_cost_var == sum(s_cost_terms))
        s_under_var = model.NewIntVar(0, 100_000_000, f's_under_{s_idx}')
        model.Add(s_under_var >= min_sal - s_cost_var)
        obj_terms.append(s_under_var * W_MIN_SALARY)

    model.Minimize(sum(obj_terms))

    # ---------- Solve ----------
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(time_limit_sec)
    solver.parameters.num_search_workers = int(num_workers)
    status = solver.Solve(model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return [], {
            'status': solver.StatusName(status),
            'objective': None,
            'wall_time_sec': solver.WallTime(),
            'request_warnings': request_warnings,
        }

    # ---------- Extract solution ----------
    assignments_out = []
    for s_idx, sdata in enumerate(staff):
        days_dict = {}
        for d in days:
            if not solver.Value(works[s_idx, d]):
                days_dict[str(d)] = ''
                continue
            slots_on = [
                t for t in range(SLOTS_PER_DAY)
                if solver.Value(assigned[s_idx, d, t])
            ]
            if not slots_on:
                days_dict[str(d)] = ''
                continue
            ss = slots_on[0]
            ee = slots_on[-1] + 1
            days_dict[str(d)] = format_slot_range(ss, ee)
        assignments_out.append({'name': sdata['name'], 'days': days_dict})

    # ---------- Compute labour cost from the solution ----------
    labor_cost_total = 0
    for s_idx, sdata in enumerate(staff):
        wage = int(sdata.get('wage') or 0)
        if wage <= 0:
            continue
        half_wage = wage // 2
        for d in days:
            for t in range(SLOTS_PER_DAY):
                if solver.Value(assigned[s_idx, d, t]):
                    labor_cost_total += half_wage

    stats = {
        'status': solver.StatusName(status),
        'objective': solver.ObjectiveValue(),
        'wall_time_sec': solver.WallTime(),
        'shortage_total': sum(
            solver.Value(shortage[d, t])
            for d in days for t in range(SLOTS_PER_DAY)
        ),
        'excess_total': sum(
            solver.Value(excess[d, t])
            for d in days for t in range(SLOTS_PER_DAY)
        ),
        'labor_cost_total': labor_cost_total,
        'target_labor_cost': target_labor_cost,
        'min_labor_cost': min_labor_cost,
        'request_warnings': request_warnings,
    }
    return assignments_out, stats


def format_slot_range(s_slot, e_slot):
    """Render a [start, end) slot pair as an 'H-H' / 'H.5-H' string (e.g. 16-24, 8.5-17)."""
    def fmt(slot):
        h = slot // 2
        return f'{h}.5' if slot % 2 == 1 else str(h)
    return f'{fmt(s_slot)}-{fmt(e_slot)}'


def hhmm_to_slot(v):
    """Parse 'HH:MM' / '18' / '18.5' into a slot index (0..48). Returns None on invalid input."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return max(0, min(SLOTS_PER_DAY, int(round(float(v) * 2))))
    s = str(v).strip()
    if not s:
        return None
    if ':' in s:
        try:
            h, m = s.split(':')[:2]
            return max(0, min(SLOTS_PER_DAY, int(h) * 2 + (1 if int(m) >= 30 else 0)))
        except Exception:
            return None
    try:
        return max(0, min(SLOTS_PER_DAY, int(round(float(s) * 2))))
    except Exception:
        return None


def parse_shift_str(val):
    """Parse '8-16' / '8.5-17' / '17-22' into (start_slot, end_slot).

    Single-range only; overnight ranges ('22-6') return (None, None).
    """
    if not val or '-' not in val:
        return None, None
    parts = val.split(',')[0].split('-')
    if len(parts) != 2:
        return None, None
    try:
        s_h = float(parts[0])
        e_h = float(parts[1])
    except Exception:
        return None, None
    s = max(0, min(SLOTS_PER_DAY, int(round(s_h * 2))))
    e = max(0, min(SLOTS_PER_DAY, int(round(e_h * 2))))
    if e <= s:
        return None, None
    return s, e
