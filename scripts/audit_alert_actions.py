#!/usr/bin/env python3
"""Machine-auditable census of frontend alert outlets and AlertAction coverage."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend" / "src"
ALERT_PATTERN = re.compile(r"AlertTriangle|AlertCircle|role=[\"']alert[\"']|toast\.(?:error|warning)")
CONTRACT_PATTERN = re.compile(r"ActionableAlert|alertActionData|data-alert-contract")
REPRESENTATIVE = {
    "pages/Quote/OnlineQuoteFlow.tsx",
    "pages/Notifications/NotificationCenter.tsx",
}


def census() -> dict:
    outlets = []
    for path in sorted(FRONTEND.rglob("*.tsx")):
        text = path.read_text(encoding="utf-8")
        matches = ALERT_PATTERN.findall(text)
        if not matches:
            continue
        relative = path.relative_to(FRONTEND).as_posix()
        outlets.append(
            {
                "file": relative,
                "alert_outlets": len(matches),
                "contracted": bool(CONTRACT_PATTERN.search(text)),
                "representative": relative in REPRESENTATIVE,
            }
        )
    return {
        "contract": "alert-action-v1",
        "files_scanned": len(list(FRONTEND.rglob("*.tsx"))),
        "files_with_alert_outlets": len(outlets),
        "outlets": outlets,
        "representative_missing_contract": sorted(
            item["file"] for item in outlets if item["representative"] and not item["contracted"]
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strict-representative", action="store_true")
    args = parser.parse_args()
    result = census()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.strict_representative and result["representative_missing_contract"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
