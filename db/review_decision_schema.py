"""[WP9-P0-7 ④ · Owner 裁决 D8]geo_article_review_events.decision 决策域放宽。

单独成模块的原因:`db/diagnosis_db.py` 在 import 时就会执行整条 `init_db()` 建表链,
判别测试不该为了验一条约束而拉起全量 schema。本模块**无 import 期副作用**,
由 `init_db()` 在启动链里调用,测试可直接、轻量地对两种起点(新库/旧库)验证同一函数。
"""
from __future__ import annotations

# 合法决策域(Owner D8 ④ 签发):skipped = company_facts/brand_softarticle 推荐人审的
# "明示跳过"(审计留痕)。扩到第四个值属漂移,schema 契约与锁测试都会拒。
REVIEW_DECISIONS = ("approved", "rejected", "skipped")


def widen_review_decision_check(cursor) -> None:
    """把 decision 的 CHECK 幂等放宽到 {approved, rejected, skipped}。

    新库由 init_db 的 inline CHECK 直接带 skipped;旧库(只含 approved/rejected)由本函数
    就地放宽。纯扩枚举域:不删任何已有值,可重复执行,不叠加重复约束。
    """
    cursor.execute("""
        DO $skip_decision$
        DECLARE con RECORD;
        BEGIN
            FOR con IN
                SELECT c.conname FROM pg_constraint c
                WHERE c.conrelid = 'geo_article_review_events'::regclass
                  AND c.contype = 'c'
                  AND pg_get_constraintdef(c.oid) ILIKE '%decision%'
                  AND pg_get_constraintdef(c.oid) NOT ILIKE '%skipped%'
            LOOP
                EXECUTE format('ALTER TABLE geo_article_review_events DROP CONSTRAINT %I', con.conname);
            END LOOP;
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conrelid = 'geo_article_review_events'::regclass
                  AND conname = 'ck_geo_article_review_decision'
            ) THEN
                ALTER TABLE geo_article_review_events
                    ADD CONSTRAINT ck_geo_article_review_decision
                    CHECK (decision IN ('approved', 'rejected', 'skipped'));
            END IF;
        END
        $skip_decision$;
    """)
