# -*- coding: utf-8 -*-
"""WO_267 判据的夹具加载。夹具 = Review 09-23 从生产只读导出的**原串**(base64,不含品牌名)。

文件格式:每行 `KEY=base64(JSON)`(`RESEARCH_B64` 调研行 19 行、`FIXTURE_B64` 品牌行业 20 行、
`ALL_B64` 全部非空 brands.industry 285 行)。sha256 钉死 —— 夹具被改过,下面的冻结读数就不再成立。
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent

FILES = {
    "prod_industry_fixtures_2026-09-23.b64.txt":
        "4111ef7aed88ac50a0804a8e6277bf3dacd3dffd39e4379382d303c75cb019cc",
    "prod_industry_all_285_2026-09-23.b64.txt":
        "9850690787caba2952a7ce0bbc053cc57a0366cb090586de2e400f3c5a67b40f",
}


def load(file_name: str, key: str):
    for line in (HERE / file_name).read_text(encoding="ascii").splitlines():
        k, _, v = line.strip().partition("=")
        if k == key:
            return json.loads(base64.b64decode(v).decode("utf-8"))
    raise KeyError("%s 里没有 %s" % (file_name, key))


def research_rows():
    return load("prod_industry_fixtures_2026-09-23.b64.txt", "RESEARCH_B64")


def brand_fixtures():
    return load("prod_industry_fixtures_2026-09-23.b64.txt", "FIXTURE_B64")


def all_285():
    return load("prod_industry_all_285_2026-09-23.b64.txt", "ALL_B64")


def context_of(row) -> list:
    """Review 导出时把「名 / notes / seed 里命中的强别名」放在 other_cols_strong_hits(品牌名本身不出库)。"""
    hits = row.get("other_cols_strong_hits")
    return [] if hits in (None, "None", "", "[]") else [str(hits)]


def sha256_of(file_name: str) -> str:
    return hashlib.sha256((HERE / file_name).read_bytes()).hexdigest()
