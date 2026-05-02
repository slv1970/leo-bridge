-- =============================================================================
-- Leo v1.6.0 — Откат миграции Корпоративной KB Респект.Чата
-- =============================================================================
-- ВНИМАНИЕ: Удаляет все данные синхронизированной KB. Если sync шёл часами —
-- придётся ждать ещё столько же при повторной заливке.
--
-- psql "$DATABASE_URL_AI" -f /opt/ai/bridge/sql/v160_rollback.sql
-- =============================================================================

BEGIN;

DROP TABLE IF EXISTS ai.respect_kb_content_attachments CASCADE;
DROP TABLE IF EXISTS ai.respect_kb_attachments        CASCADE;
DROP TABLE IF EXISTS ai.respect_kb                    CASCADE;
DROP TABLE IF EXISTS ai.respect_kb_sync_log           CASCADE;

COMMIT;

\echo '=== Таблицы respect_kb_* удалены ==='
SELECT tablename FROM pg_tables WHERE schemaname='ai' AND tablename LIKE 'respect_kb%';
