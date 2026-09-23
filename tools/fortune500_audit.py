from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from urllib.request import urlopen


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.company_aliases import company_key, normalize_company_name
from src.config import Config
from src.database import Database
from src.main import _resolve_boards_csv


FORTUNE_500_DATASET_URL = "https://www.salttechno.ai/datasets/fortune-500-companies-2025.json"
MAIN_SOURCE_COMPANIES = {
    "Amazon",
    "Apple",
    "Google",
    "Goldman Sachs",
    "IBM",
    "LinkedIn",
    "Meta",
    "Microsoft",
    "Netflix",
    "NVIDIA",
    "Oracle",
    "Stripe",
}


def _load_fortune500_dataset(source: str) -> list[dict]:
    if source.startswith(("http://", "https://")):
        with urlopen(source, timeout=20) as response:
            payload = json.load(response)
    else:
        with open(source, encoding="utf-8") as handle:
            payload = json.load(handle)
    if isinstance(payload, dict):
        records = payload.get("data") or payload.get("companies") or payload.get("items") or []
    else:
        records = payload
    if not isinstance(records, list):
        raise ValueError("Fortune 500 dataset payload does not contain a list of companies")
    return records


def _extract_company_name(record: dict) -> str:
    for key in ("company", "companyName", "name", "title"):
        value = record.get(key)
        if value:
            return str(value).strip()
    return ""


def _load_board_companies(path: str) -> set[str]:
    companies: set[str] = set()
    with open(path, encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            company = (row.get("company_name") or row.get("company") or "").strip()
            ok_val = (row.get("ok") or "").strip().lower()
            if ok_val and ok_val not in ("true", "1", "yes"):
                continue
            if company:
                companies.add(company)
    return companies


def _load_board_health() -> dict[str, str]:
    cfg = Config.load()
    db = Database(cfg.database.path)
    try:
        boards = db.list_boards(limit=10000)
    finally:
        db.close()
    health_by_key: dict[str, str] = {}
    rank = {"active": 3, "degraded": 2, "dead": 1}
    for board in boards:
        key = company_key(board.get("company") or "")
        status = str(board.get("status") or "").strip().lower() or "listed"
        if not key:
            continue
        current = health_by_key.get(key)
        if current is None or rank.get(status, 0) > rank.get(current, 0):
            health_by_key[key] = status
    return health_by_key


def _report_row(name: str, inventory_names: set[str], inventory_keys: set[str], board_health: dict[str, str]) -> dict:
    raw_name = name.strip()
    normalized_name = normalize_company_name(raw_name)
    normalized_key = company_key(raw_name)
    board_status = board_health.get(normalized_key, "")
    exact_listed = raw_name in inventory_names
    alias_listed = normalized_key in inventory_keys
    normalized_main = normalized_name in MAIN_SOURCE_COMPANIES

    if exact_listed:
        if board_status:
            status = f"board-{board_status}"
        elif normalized_main:
            status = "main-source"
        else:
            status = "board-listed"
    elif alias_listed:
        if board_status:
            status = f"alias-board-{board_status}"
        elif normalized_main:
            status = "alias-main-source"
        else:
            status = "alias-board-listed"
    elif normalized_main:
        status = "alias-main-source" if normalized_name != raw_name else "main-source"
    else:
        status = "missing"
    return {
        "fortune500_company": raw_name,
        "normalized_company": normalized_name,
        "status": status,
    }


def main() -> int:
    dataset_source = sys.argv[1] if len(sys.argv) > 1 else FORTUNE_500_DATASET_URL
    boards_csv = _resolve_boards_csv("")
    fortune500_records = _load_fortune500_dataset(dataset_source)
    fortune500_names = [name for name in (_extract_company_name(row) for row in fortune500_records) if name]

    inventory_names = _load_board_companies(boards_csv)
    inventory_names.update(MAIN_SOURCE_COMPANIES)
    inventory_keys = {company_key(name) for name in inventory_names if name}
    board_health = _load_board_health()

    rows = [_report_row(name, inventory_names, inventory_keys, board_health) for name in fortune500_names]
    status_counts: dict[str, int] = {}
    for row in rows:
        status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1

    report_dir = ROOT_DIR / "state"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "fortune500_audit.csv"
    with open(report_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["fortune500_company", "normalized_company", "status"],
        )
        writer.writeheader()
        writer.writerows(rows)

    print(f"Boards CSV: {boards_csv}")
    print(f"Fortune 500 source: {dataset_source}")
    for status, count in sorted(status_counts.items()):
        print(f"{status}: {count}")
    print(f"Report written: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
