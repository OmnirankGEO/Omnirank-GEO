"""Retired legacy ranking prompt updater.

The old updater could restore customer-first scoring and unsupported authority claims.
It is intentionally non-operational; new generation uses the six-family evidence contract.
"""


def main() -> None:
    raise SystemExit(
        "retired: use writing.article_style_contract and canonical_family_templates"
    )


if __name__ == "__main__":
    main()
