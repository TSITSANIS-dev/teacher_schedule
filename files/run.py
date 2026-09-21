import argparse
import re
import shutil
import subprocess
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk
from datetime import date
import difflib
import os
import pandas as pd
from substitute_teacher import (COMPLETED, OPEN, SCHEDULED, SUBSTITUTED,
                                compact_open_periods, cover_absences, mark_completed, new_log)

HERE = Path(__file__).parent
DATA, OUT = HERE, HERE / "output"

WEEKDAY_ALIASES = {
    "monday": "monday", "mon": "monday",
    "tuesday": "tuesday", "tue": "tuesday", "tues": "tuesday",
    "wednesday": "wednesday", "wed": "wednesday", "wends": "wednesday",
    "wendsday": "wednesday", "wendsay": "wednesday", "wenesday": "wednesday",
    "thursday": "thursday", "thu": "thursday", "thur": "thursday", "thurs": "thursday",
    "friday": "friday", "fri": "friday"
}

def normalize_weekday(value):
    key = "".join(str(value).strip().lower().split()).strip(".,;:")
    if key in WEEKDAY_ALIASES:
        return WEEKDAY_ALIASES[key]
    close = difflib.get_close_matches(key, list(WEEKDAY_ALIASES), n=1, cutoff=0.7)
    return WEEKDAY_ALIASES[close[0]] if close else key

def read_csv(path, required, int_cols=()):
    if not path.exists():
        raise ValueError(f"File not found: {path}")

    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin1"):
        for sep in (",", ";", "\t"):
            try:
                df = pd.read_csv(path, dtype=str, encoding=encoding, sep=sep,
                                 skipinitialspace=True, engine="python")
                break
            except Exception:
                continue
        else:
            continue
        break
    else:
        raise ValueError(f"Could not read {path.name} with common encodings/delimiters")

    df.columns = (df.columns
                  .str.replace("\ufeff", "", regex=False)
                  .str.replace("\xa0", " ", regex=False)
                  .str.strip()
                  .str.lower()
                 )

    df = df.dropna(how="all").reset_index(drop=True)

    required_lower = [c.lower() for c in required]
    missing = [c for c in required_lower if c not in df.columns]
    if missing:
        raise ValueError(
            f"{path.name}: missing column(s): {', '.join(missing)}\n"
            f"Found columns: {list(df.columns)}"
        )

    for col in df.columns:
        df[col] = df[col].str.strip()

    for col in int_cols:
        try:
            df[col] = df[col].astype(int)
        except (ValueError, TypeError):
            raise ValueError(f"{path.name}: column '{col}' must contain whole numbers")

    return df

def load_day(date, reset):
    saved, saved_log = OUT / f"{date}_schedule.csv", OUT / f"{date}_log.csv"
    if saved.exists() and not reset:
        schedule = read_csv(saved, ["date", "class_id", "period", "subject", "teacher_id", "status"],
                            ["period"])
        log = pd.read_csv(saved_log, dtype={"date": str}) if saved_log.exists() else new_log()
        return schedule, log

    timetable = read_csv(DATA / "timetable.csv",
                         ["day", "class_id", "period", "subject", "teacher_id"],
                         ["period"])
    day_name = pd.Timestamp(date).day_name()
    day = normalize_weekday(day_name)
    schedule = timetable[timetable["day"].apply(normalize_weekday) == day]
    if schedule.empty:
        raise ValueError(f"timetable.csv has no lessons for {day_name} ({date})")
    schedule = schedule.drop(columns="day").reset_index(drop=True)
    schedule.insert(0, "date", date)
    schedule["status"] = SCHEDULED
    return schedule, new_log()


def check(schedule, teacher_subjects, absences):
    dup = schedule[schedule.duplicated(["class_id", "period"], keep=False)]
    if not dup.empty:
        raise ValueError("A class has two lessons in the same period:\n" + dup.to_string(index=False))
    known = {s.casefold() for s in teacher_subjects["subject"].dropna()}
    sched_subjects = {s.casefold(): s for s in schedule["subject"].dropna()}
    no_teacher = sorted(orig for key, orig in sched_subjects.items() if key not in known)

    warnings = []
    if no_teacher:
        warnings.append("No teacher is associated with: " + ", ".join(no_teacher))
    unknown = sorted(set(absences["teacher_id"]) - set(schedule["teacher_id"].dropna()))
    if unknown:
        warnings.append("Absent teacher(s) with no lessons in today's schedule "
                        "(typo in the name, or already covered): " + ", ".join(unknown))
    return warnings


def build_grid(schedule):
    d = schedule.copy()

    def cell(r):
        teacher = r["teacher_id"] if isinstance(r["teacher_id"], str) else "OPEN"
        mark = {SUBSTITUTED: " *sub", COMPLETED: " (done)"}.get(r["status"], "")
        return f"{r['subject']}/{teacher}{mark}"

    d["cell"] = d.apply(cell, axis=1)
    return d.pivot(index="class_id", columns="period", values="cell").fillna("-")


def format_grid(schedule, title):
    return f"{title}\n{build_grid(schedule).to_string()}"


def open_in_calc(path):
    exe = shutil.which("soffice") or shutil.which("libreoffice")
    if not exe:
        return False
    try:
        subprocess.Popen([exe, "--calc", str(path)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False


ABSENT_MARK, PRESENT_MARK = "x", "-"
SUBJECT_SEP = ";"


def expand_subjects(teacher_subjects):
    exploded = teacher_subjects.assign(subject=teacher_subjects["subject"].str.split(SUBJECT_SEP))
    exploded = exploded.explode("subject", ignore_index=True)
    exploded["subject"] = exploded["subject"].str.strip()
    return exploded


def absent_teacher_ids(teacher_subjects):
    flag = teacher_subjects["absent"].str.strip().str.lower()
    bad = teacher_subjects[~flag.isin([ABSENT_MARK, PRESENT_MARK])]
    if not bad.empty:
        raise ValueError("teacher_subjects.csv: 'absent' column must be "
                         f"'{ABSENT_MARK}' or '{PRESENT_MARK}', found:\n" + bad.to_string(index=False))

    per_teacher = flag.groupby(teacher_subjects["teacher_id"]).nunique()
    contradictory = sorted(per_teacher[per_teacher > 1].index)
    if contradictory:
        raise ValueError("teacher_subjects.csv: teacher_id(s) marked both absent and present "
                         "on different rows - make it consistent for: " + ", ".join(contradictory))

    return sorted(set(teacher_subjects.loc[flag == ABSENT_MARK, "teacher_id"]))

def read_occupied_hours(path, valid_teacher_ids, max_period):
    empty = pd.DataFrame(columns=["day", "teacher_id", "hours"])
    notes = []

    if not path.exists():
        return empty, notes

    try:
        df = pd.read_csv(path, dtype=str, skipinitialspace=True)
    except Exception as e:
        raise ValueError(f"Could not read {path.name}: {e}")

    df.columns = df.columns.str.strip().str.lower()

    if "weekday" in df.columns:
        df = df.rename(columns={"weekday": "day"})
    if "day" not in df.columns or "teacher_id" not in df.columns or "hours" not in df.columns:
        raise ValueError(
            f"{path.name}: expected columns 'day' (or 'day'), 'teacher_id', 'hours'. "
            f"Found: {list(df.columns)}"
        )

    df = df.dropna(how="all").reset_index(drop=True)
    for col in df.columns:
        df[col] = df[col].astype(str).str.strip()

    valid = {str(t) for t in valid_teacher_ids}
    unknown = sorted(set(df["teacher_id"]) - valid)
    if unknown:
        notes.append(f"{path.name}: ignored unknown teacher_id(s): {', '.join(unknown)}")
        df = df[df["teacher_id"].isin(valid)]

    rows = []
    for idx, row in df.iterrows():
        day = normalize_weekday(row["day"])
        if day not in WEEKDAY_ALIASES.values():
            notes.append(f"{path.name} line {idx+2}: unrecognized day '{row['day']}'")
            continue

        tid = row["teacher_id"]
        raw_hours = re.split(r"[,;\s]+", row["hours"])
        hours = []
        for h in raw_hours:
            h = h.strip()
            if not h:
                continue
            try:
                period = int(h)
                if 1 <= period <= max_period:
                    hours.append(period)
                else:
                    notes.append(f"{path.name} line {idx+2}: period {period} out of range 1-{max_period}")
            except ValueError:
                notes.append(f"{path.name} line {idx+2}: invalid hour value '{h}'")

        for period in hours:
            rows.append({"day": day, "teacher_id": tid, "period": period})

    occupied = pd.DataFrame(rows, columns=["day", "teacher_id", "period"]) if rows else empty
    return occupied, notes

def run_pipeline(date_str, last_finished, max_subs, reset):
    date = str(pd.Timestamp(date_str).date())

    schedule, log = load_day(date, reset)
    schedule["period"] = pd.to_numeric(schedule["period"], errors="coerce").astype("Int64")

    teacher_subjects_raw = read_csv(DATA / "teacher_subjects.csv", ["teacher_id", "subject", "absent"])
    absent_ids = absent_teacher_ids(teacher_subjects_raw)
    teacher_subjects = expand_subjects(teacher_subjects_raw)

    if last_finished is not None:
        schedule = mark_completed(schedule, date, last_finished)

    last_period = int(schedule["period"].max())
    day = normalize_weekday(pd.Timestamp(date).day_name())
    occupied, occ_notes = read_occupied_hours(DATA / "teacher_schedule.csv",
                                              teacher_subjects_raw["teacher_id"].unique(), last_period)
    if absent_ids:
        absences = pd.DataFrame({
            "date": date,
            "teacher_id": absent_ids,
            "first_period": 1,
            "last_period": last_period,
        })
    else:
        absences = pd.DataFrame(columns=["date", "teacher_id", "first_period", "last_period"])

    warnings = check(schedule, teacher_subjects, absences)
    warnings.extend(occ_notes)
    if not absent_ids:
        warnings.insert(0, "No teacher is marked absent ('x') in teacher_subjects.csv - nothing to cover.")

    sections = [format_grid(schedule, f"BEFORE ({date})")]
    schedule, log = cover_absences(schedule, teacher_subjects, absences, log, date, max_subs,
                                   occupied=occupied, weekday=day)
    schedule = compact_open_periods(schedule, date, occupied=occupied, weekday=day)
    sections.append(format_grid(schedule, "AFTER  (*sub = substitute assigned, OPEN pushed as late as possible)"))

    OUT.mkdir(exist_ok=True)
    schedule.to_csv(OUT / f"{date}_schedule.csv", index=False)
    log.to_csv(OUT / f"{date}_log.csv", index=False)


    grid_path = OUT / f"{date}_schedule_grid.csv"
    build_grid(schedule).to_csv(grid_path)
    opened = open_in_calc(grid_path)

    if warnings:
        sections.append("WARNINGS:\n" + "\n".join(f" - {w}" for w in warnings))
    if not log.empty:
        sections.append("Substitution log:\n" + log.to_string(index=False))

    still_open = schedule[schedule["status"] == OPEN]
    if not still_open.empty:
        sections.append("Needs admin attention:\n"
                        + still_open[["class_id", "period", "subject"]].to_string(index=False))

    sections.append(f"Saved to {OUT}")
    sections.append(f"Updated schedule opened in LibreOffice Calc: {grid_path}" if opened
                    else f"Updated schedule grid saved to {grid_path} "
                         "(couldn't auto-open LibreOffice Calc - open it manually)")
    return {"report": "\n\n".join(sections), "grid_path": grid_path}


class App(tk.Tk):

    def __init__(self, defaults):
        super().__init__()
        self.title("Substitute Teacher Cover")
        self.geometry("760x560")
        self.minsize(560, 400)

        form = ttk.Frame(self, padding=10)
        form.pack(fill="x")

        ttk.Label(form, text="Date (YYYY-MM-DD):").grid(row=0, column=0, sticky="w")
        self.date_var = tk.StringVar(value=defaults["date"])
        ttk.Entry(form, textvariable=self.date_var, width=14).grid(row=0, column=1, sticky="w", padx=(4, 16))

        ttk.Label(form, text="Last finished period:").grid(row=0, column=2, sticky="w")
        self.last_var = tk.StringVar(
            value="" if defaults["last_finished"] is None else str(defaults["last_finished"]))
        ttk.Entry(form, textvariable=self.last_var, width=6).grid(row=0, column=3, sticky="w", padx=(4, 16))

        ttk.Label(form, text="Max subs/teacher:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.max_var = tk.StringVar(value=str(defaults["max_subs"]))
        ttk.Entry(form, textvariable=self.max_var, width=6).grid(row=1, column=1, sticky="w", padx=(4, 16), pady=(6, 0))

        self.reset_var = tk.BooleanVar(value=defaults["reset"])
        ttk.Checkbutton(form, text="Ignore saved state", variable=self.reset_var).grid(
            row=1, column=2, columnspan=2, sticky="w", pady=(6, 0))

        ttk.Button(form, text="Run", command=self.run).grid(row=0, column=4, rowspan=2, padx=(10, 0), sticky="ns")

        self.grid_path = None
        self.calc_btn = ttk.Button(form, text="Open in Calc", command=self.reopen_in_calc, state="disabled")
        self.calc_btn.grid(row=0, column=5, rowspan=2, padx=(10, 0), sticky="ns")

        self.output = scrolledtext.ScrolledText(self, wrap="word", font=("Courier New", 10))
        self.output.pack(fill="both", expand=True, padx=10, pady=(4, 4))
        self.output.configure(state="disabled")

        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(self, textvariable=self.status_var, anchor="w", padding=(10, 0, 10, 8)).pack(fill="x")

        self.run()

    def _show(self, text):
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.insert("end", text)
        self.output.configure(state="disabled")

    def run(self):
        last_raw = self.last_var.get().strip()
        try:
            last_finished = int(last_raw) if last_raw else None
            max_subs = int(self.max_var.get().strip())
        except ValueError:
            messagebox.showerror("Invalid input", "Last finished period and Max subs must be whole numbers.")
            return

        try:
            result = run_pipeline(self.date_var.get().strip(), last_finished, max_subs, self.reset_var.get())
        except Exception as exc:
            messagebox.showerror("Could not run", str(exc))
            self.status_var.set("Failed - see error dialog.")
            return

        self._show(result["report"])
        self.grid_path = result["grid_path"]
        self.calc_btn.configure(state="normal")
        self.status_var.set(f"Done. Output saved to {OUT}")

    def reopen_in_calc(self):
        if self.grid_path and not open_in_calc(self.grid_path):
            messagebox.showerror("Could not open Calc",
                                 "LibreOffice Calc isn't available or couldn't be launched.")


def main():
    ap = argparse.ArgumentParser(description="Cover teacher absences for one day (opens a small GUI).")
    ap.add_argument("--date", default=str(pd.Timestamp.today().date()),
                    help="YYYY-MM-DD (default: today)")
    ap.add_argument("--last-finished", type=int, default=None,
                    help="last period that has already ended today (those lessons count as completed)")
    ap.add_argument("--max-subs", type=int, default=2,
                    help="max substitutions per teacher per day (default 2)")
    ap.add_argument("--reset", action="store_true", help="ignore the saved state for this date")
    args = ap.parse_args()

    defaults = {"date": args.date, "last_finished": args.last_finished,
               "max_subs": args.max_subs, "reset": args.reset}
    App(defaults).mainloop()


if __name__ == "__main__":
    main()
