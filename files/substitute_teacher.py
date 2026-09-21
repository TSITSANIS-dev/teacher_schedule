import numpy as np
import pandas as pd

SCHEDULED, COMPLETED, SUBSTITUTED, OPEN = "Scheduled", "Completed", "Substituted", "Open"
LOG_COLUMNS = ["date", "class_id", "period", "absent_teacher", "substitute",
               "subject", "moved_from_period", "freed_hour"]
CANDIDATE_COLUMNS = ["moved_from_period", "subject", "own_teacher", "teacher_id", "score"]


# --------------------------------------------------------------------------- helpers
def new_log():
    return pd.DataFrame(columns=LOG_COLUMNS)


def mark_completed(schedule, date, last_finished_period):
    s = schedule.copy()
    done = ((s["date"] == date) & (s["period"] <= last_finished_period)
            & (s["status"] == SCHEDULED))
    s.loc[done, "status"] = COMPLETED
    return s


def _hour(s, date, class_id, period):
    return (s["date"] == date) & (s["class_id"] == class_id) & (s["period"] == period)


def _set(s, mask, subject, teacher, status):
    s.loc[mask, "subject"] = subject
    s.loc[mask, "teacher_id"] = teacher
    s.loc[mask, "status"] = status


def _absent_at(absences, date, teacher_ids, period):
    a = absences[(absences["date"] == date)
                 & (absences["first_period"] <= period)
                 & (absences["last_period"] >= period)]
    return np.isin(np.asarray(teacher_ids, dtype=object),
                   a["teacher_id"].to_numpy(dtype=object))


def _occupied_at(occupied, weekday, teacher_ids, period):
    if occupied is None or occupied.empty or weekday is None:
        return np.zeros(len(teacher_ids), dtype=bool)
    o = occupied[(occupied["day"] == weekday) & (occupied["period"] == period)]
    return np.isin(np.asarray(teacher_ids, dtype=object),
                   o["teacher_id"].to_numpy(dtype=object))


# --------------------------------------------------------------------------- core
def find_candidates(schedule, teacher_subjects, absences, log, date, class_id, period,
                    max_subs_per_day=2, occupied=None, weekday=None):
    """Ranked replacement candidates for one absent lesson (best first)."""
    if not _hour(schedule, date, class_id, period).any():
        raise ValueError(f"No lesson for {class_id}, period {period}, on {date}")
    day = schedule[schedule["date"] == date]

    # 1) subjects the class still has to do: its other hours that are not completed
    open_lessons = day[(day["class_id"] == class_id)
                       & (day["period"] != period)
                       & (day["status"] == SCHEDULED)]
    open_lessons = open_lessons.rename(columns={"period": "moved_from_period",
                                                "teacher_id": "own_teacher"})
    open_lessons = open_lessons[["moved_from_period", "subject", "own_teacher"]]

    ts_key = teacher_subjects.assign(_key=teacher_subjects["subject"].str.casefold())[["_key", "teacher_id"]]
    pool = (open_lessons.assign(_key=open_lessons["subject"].str.casefold())
                       .merge(ts_key, on="_key", how="inner")
                       .drop(columns="_key"))

    busy = day.loc[day["period"] == period, "teacher_id"].dropna().to_numpy(dtype=object)
    pool["load"] = pool["teacher_id"].map(day.groupby("teacher_id").size()).fillna(0)
    pool["subs_today"] = pool["teacher_id"].map(
        log[log["date"] == date].groupby("substitute").size()).fillna(0)
    keep = (~pool["teacher_id"].isin(busy)
            & ~_absent_at(absences, date, pool["teacher_id"], period)
            & ~_occupied_at(occupied, weekday, pool["teacher_id"], period)
            & (pool["subs_today"] < max_subs_per_day))
    pool = pool[keep].copy()
    if pool.empty:
        return pd.DataFrame(columns=CANDIDATE_COLUMNS)

    # 4) score (weights are easy to tune)
    own = (pool["teacher_id"] == pool["own_teacher"]).to_numpy()
    dist = np.abs(pool["moved_from_period"].to_numpy() - period)
    load = pool["load"].to_numpy(dtype=float)
    subs = pool["subs_today"].to_numpy(dtype=float)
    pool["score"] = (40 * own
                     + 20 * (1 - load / max(load.max(), 1))
                     + 20 * (1 - subs / max_subs_per_day)
                     + 10 / (1 + dist))
    return (pool.sort_values(["score", "moved_from_period"], ascending=[False, True])
                .reset_index(drop=True)[CANDIDATE_COLUMNS])


def assign_replacement(schedule, teacher_subjects, absences, log, date, class_id, period,
                       max_subs_per_day=2, occupied=None, weekday=None):
    """Assign the best candidate. Returns (schedule, log, chosen); chosen is None if nobody fits."""
    cands = find_candidates(schedule, teacher_subjects, absences, log, date, class_id, period,
                            max_subs_per_day, occupied, weekday)
    if cands.empty:
        return schedule, log, None

    best = cands.iloc[0]
    src = int(best["moved_from_period"])
    s = schedule.copy()
    absent_lesson = s[_hour(s, date, class_id, period)].iloc[0]
    absent_teacher = absent_lesson["teacher_id"]

    _set(s, _hour(s, date, class_id, period), best["subject"], best["teacher_id"], SUBSTITUTED)

    freed = _hour(s, date, class_id, src)
    _set(s, freed, absent_lesson["subject"], None, OPEN)
    a_free = (not _absent_at(absences, date, [absent_teacher], src)[0]
              and not _occupied_at(occupied, weekday, [absent_teacher], src)[0]
              and s[(s["date"] == date) & (s["period"] == src)
                    & (s["teacher_id"] == absent_teacher)].empty)
    if a_free:
        _set(s, freed, absent_lesson["subject"], absent_teacher, SCHEDULED)

    row = pd.DataFrame([{
        "date": date, "class_id": class_id, "period": period,
        "absent_teacher": absent_teacher, "substitute": best["teacher_id"],
        "subject": best["subject"], "moved_from_period": src,
        "freed_hour": "absent lesson moved here" if a_free else "OPEN - needs cover"}])
    log = row if log.empty else pd.concat([log, row], ignore_index=True)
    return s, log, best


def cover_absences(schedule, teacher_subjects, absences, log, date, max_subs_per_day=2,
                   occupied=None, weekday=None):
    """Cover every absent lesson of the day, earliest period first. Returns (schedule, log)."""
    s = schedule.copy()
    todo = []
    for ab in absences[absences["date"] == date].itertuples():
        m = ((s["date"] == date) & (s["teacher_id"] == ab.teacher_id)
             & s["period"].between(ab.first_period, ab.last_period)
             & (s["status"] == SCHEDULED))
        todo += list(s.loc[m, ["period", "class_id"]].itertuples(index=False, name=None))

    for period, class_id in sorted(todo):
        row = s[_hour(s, date, class_id, period)].iloc[0]
        if row["status"] != SCHEDULED or not _absent_at(absences, date, [row["teacher_id"]], period)[0]:
            continue
        s, log, best = assign_replacement(s, teacher_subjects, absences, log, date,
                                          class_id, period, max_subs_per_day, occupied, weekday)
        if best is None:
            _set(s, _hour(s, date, class_id, period), row["subject"], None, OPEN)
    return s, log


def compact_open_periods(schedule, date, occupied=None, weekday=None):
    s = schedule.copy()
    for class_id in s.loc[s["date"] == date, "class_id"].unique():
        changed = True
        while changed:
            changed = False
            mask = (s["date"] == date) & (s["class_id"] == class_id)
            day = s.loc[mask].sort_values("period")
            for p in day.loc[day["status"] == OPEN, "period"]:
                later_filled = day[(day["period"] > p) & (day["status"] != OPEN)]
                later_filled = later_filled.sort_values("period", ascending=False)
                for q_idx, cand in later_filled.iterrows():
                    if _occupied_at(occupied, weekday, [cand["teacher_id"]], p)[0]:
                        continue
                    p_idx = day.index[day["period"] == p][0]
                    for col in ("subject", "teacher_id", "status"):
                        s.at[p_idx, col], s.at[q_idx, col] = s.at[q_idx, col], s.at[p_idx, col]
                    changed = True
                    break
                if changed:
                    break
    return s
