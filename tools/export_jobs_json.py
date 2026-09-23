"""Export recent jobs from all Job Radar state DBs to one JSON file.

Consumer: Likhith's h1b-100co-job-check scheduled task (7 & 11 AM ET), which reads
https://raw.githubusercontent.com/likithreddy25/job-radar/main/state/jobs_export.json

Every job seen in the last --days days is re-scored at export time with the CURRENT
classifier + profile + candidate evidence (so scores stay correct even for rows the
DBs stored under an older profile). Output fields per job:

  key, source, company, title, location, url, posted, first_seen (UTC ISO),
  title_label ("yes"/"maybe"/"no"), score, label, grade, years_required (int, 0 = none found),
  salary_text (first $ range found in the JD, "" if none), critical_skill_gaps,
  blocked_reason ("" unless clearance / citizenship / no-sponsorship / 4+ years / location),
  fit_summary, jd_excerpt (first --jd-chars chars of the JD, "" if none).

Only title_label yes/maybe jobs are written, sorted newest first.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.classifier import classify  # noqa: E402
from src import evaluation as ev  # noqa: E402

DEFAULT_DBS = [
    "state/gha-jobs.db",
    "state/gha-boards.db",
    "state/gha-boards-broad-non-workday.db",
]
_SALARY_RE = re.compile(
    r"\$\s?\d{2,3}(?:,\d{3}|\.\d+)?\s?[kK]?(?:\s*(?:-|–|—|to)\s*\$?\s?\d{2,3}(?:,\d{3}|\.\d+)?\s?[kK]?)?"
)
_BLOCK_HINTS = (
    "clearance", "citizen", "sponsor", "u.s. person", "itar", "years of experience",
    "outside the us", "non-us", "location",
)


def _rows(db_path: Path, cutoff: str):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    seen_col = "first_seen" if "first_seen" in cols else None
    if not seen_col:
        conn.close()
        return []
    rows = conn.execute(
        f"SELECT key, source, company, title, location, url, posted, description, {seen_col} AS first_seen "
        f"FROM jobs WHERE {seen_col} >= ? AND COALESCE(manual_input, 0) = 0",
        (cutoff,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="state/jobs_export.json")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--jd-chars", type=int, default=6000)
    ap.add_argument("dbs", nargs="*", default=DEFAULT_DBS)
    args = ap.parse_args()

    cutoff = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat()
    merged: dict[str, dict] = {}
    for db in args.dbs:
        path = ROOT / db
        if not path.exists():
            print(f"skip {db}: not found")
            continue
        for row in _rows(path, cutoff):
            prev = merged.get(row["key"])
            if prev is None or (row["first_seen"] or "") < (prev["first_seen"] or ""):
                merged[row["key"]] = row

    out = []
    for row in merged.values():
        title = row.get("title") or ""
        cls = classify(title)
        if cls.label not in ("yes", "maybe"):
            continue
        desc = row.get("description") or ""
        result = ev.evaluate_job(
            title,
            desc,
            company=row.get("company") or "",
            location=row.get("location") or "",
            source=row.get("source") or "",
            use_llm=False,
        )
        blocked = ""
        if result.label == "no":
            for reason in result.reasons:
                if any(h in reason.lower() for h in _BLOCK_HINTS):
                    blocked = reason
                    break
        salary = _SALARY_RE.search(desc)
        out.append(
            {
                "key": row["key"],
                "source": row.get("source") or "",
                "company": row.get("company") or "",
                "title": title,
                "location": row.get("location") or "",
                "url": row.get("url") or "",
                "posted": row.get("posted") or "",
                "first_seen": row.get("first_seen") or "",
                "title_label": cls.label,
                "score": result.score,
                "label": result.label,
                "grade": result.grade,
                "years_required": ev._extract_years_requirement(desc) if desc else 0,
                "salary_text": salary.group(0).strip() if salary else "",
                "critical_skill_gaps": list(result.critical_skill_gaps),
                "blocked_reason": blocked,
                "fit_summary": result.fit_summary,
                "jd_excerpt": desc[: args.jd_chars],
            }
        )

    out.sort(key=lambda j: j["first_seen"], reverse=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": args.days,
        "count": len(out),
        "jobs": out,
    }
    out_path = ROOT / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    labels = {}
    for j in out:
        labels[j["label"]] = labels.get(j["label"], 0) + 1
    print(f"Wrote {len(out)} jobs to {args.out} ({labels}); {out_path.stat().st_size / 1048576:.1f} MiB")


if __name__ == "__main__":
    main()
