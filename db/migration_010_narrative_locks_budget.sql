CREATE TABLE IF NOT EXISTS narrative_enrichment_locks (
    diagnosis_id INTEGER PRIMARY KEY REFERENCES diagnosis_records(id),
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'succeeded', 'succeeded_partial', 'failed_full')),
    retries INTEGER NOT NULL DEFAULT 0,
    token_cost_yuan NUMERIC(10, 4) DEFAULT 0,
    error_message TEXT,
    worker_id TEXT,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    last_heartbeat_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- @index-guard idx_narrative_locks_status ON narrative_enrichment_locks plain
DO $idxguard$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
                WHERE c.relname = 'idx_narrative_locks_status' AND i.indrelid = to_regclass('public.narrative_enrichment_locks')) THEN
        NULL;  -- 已在 public.narrative_enrichment_locks 上 → 幂等跳过
    ELSIF EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.relname = 'idx_narrative_locks_status' AND c.relnamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION '[index-guard] idx_narrative_locks_status 已存在但不在 public.narrative_enrichment_locks 上(实际宿主:%)—— 拒绝静默跳过',
            (SELECT COALESCE(t.relname, 'relkind=' || c.relkind::text) FROM pg_class c
               LEFT JOIN pg_index i ON i.indexrelid = c.oid
               LEFT JOIN pg_class t ON t.oid = i.indrelid
              WHERE c.relname = 'idx_narrative_locks_status' AND c.relnamespace = 'public'::regnamespace)
            USING ERRCODE = 'duplicate_object';
    ELSE
        CREATE INDEX idx_narrative_locks_status ON public.narrative_enrichment_locks (status, updated_at);
    END IF;
END $idxguard$;

CREATE TABLE IF NOT EXISTS narrative_monthly_budget (
    month_key TEXT PRIMARY KEY,
    total_yuan NUMERIC(10, 4) NOT NULL DEFAULT 0,
    enrichment_count INTEGER NOT NULL DEFAULT 0,
    last_updated_at TIMESTAMP DEFAULT NOW()
);
