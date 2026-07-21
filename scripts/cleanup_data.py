"""Reset local runtime data for a fresh SignalOps scenario.

Removes the SQLite incident database (including SQLite sidecar files) and all
approved KB Markdown articles. It never changes source code, configuration, or
the base knowledge patterns.
"""
from __future__ import annotations

import argparse
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
INCIDENT_DB = PROJECT_ROOT / "data" / "incidents.db"
KB_DIR = PROJECT_ROOT / "knowledge" / "approved"


def targets() -> list[Path]:
    database_files = [Path(f"{INCIDENT_DB}{suffix}") for suffix in ("", "-wal", "-shm")]
    kb_files = list(KB_DIR.glob("*.md")) if KB_DIR.exists() else []
    return [path for path in database_files + kb_files if path.exists()]


def main() -> None:
    parser = argparse.ArgumentParser(description="Delete local incidents and approved KB articles.")
    parser.add_argument("--yes", action="store_true", help="Confirm deletion without an interactive prompt.")
    args = parser.parse_args()

    selected = targets()
    if not selected:
        print("No incident or approved KB data found.")
        return

    print("The following local runtime data will be permanently removed:")
    for path in selected:
        print(f"- {path.relative_to(PROJECT_ROOT)}")
    if not args.yes and input("Type DELETE to continue: ").strip() != "DELETE":
        print("Cancelled. No data was removed.")
        return

    for path in selected:
        path.unlink()
    print(f"Removed {len(selected)} item(s). The next incident will start from a fresh database.")


if __name__ == "__main__":
    main()
