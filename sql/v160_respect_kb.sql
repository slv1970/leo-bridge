-- =============================================================================
-- Leo v1.6.0 — Корпоративная KB Респект.Чата
-- Миграция: создание таблиц для синхронизированной копии и FTS-поиска
-- =============================================================================
-- Применять под пользователем ai_user в БД ai (DATABASE_URL_AI):
--   psql "$DATABASE_URL_AI" -f /opt/ai/bridge/sql/v160_respect_kb.sql
-- Откат: см. v160_rollback.sql
-- =============================================================================

BEGIN;

-- ----------------------------------------------------------------------------
-- 1. Основная таблица: карточки контента из Респект.Чата
--    Источник истины — БД Респект.Чата. Здесь — синхронизированная копия
--    + извлечённый текст для FTS, + обогащённый текст из распарсенных файлов.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ai.respect_kb (
    -- идентификатор записи на стороне Респект.Чата (PK)
    content_id           BIGINT       PRIMARY KEY,

    -- собственно карточка
    title                TEXT         NOT NULL,
    body_html            TEXT         NOT NULL DEFAULT '',
    body_plain           TEXT         NOT NULL DEFAULT '',   -- из body_html без тегов, для preview
    indexable_text       TEXT         NOT NULL DEFAULT '',   -- body_plain + тексты parseable attachments, для FTS

    -- структура и метаданные
    section_path         TEXT[],                              -- ["КОНКУРЕНТЫ", "Экспресс анализ", ...]
    cover_image_url      TEXT,                                -- картинка карточки (если есть)
    actualized_at        TIMESTAMPTZ,                         -- "Дата актуализации" из карточки
    updated_at           TIMESTAMPTZ NOT NULL,                -- last modification на стороне Респект.Чата
    synced_at            TIMESTAMPTZ NOT NULL DEFAULT now(),  -- когда мы синхронизировали

    -- FTS-индекс. Русская конфигурация подходит для смешанного русско-английского текста.
    fts                  TSVECTOR
                         GENERATED ALWAYS AS (
                             setweight(to_tsvector('russian', coalesce(title, '')), 'A')
                          || setweight(to_tsvector('russian', coalesce(indexable_text, '')), 'B')
                         ) STORED
);

-- GIN-индекс по FTS — основа поиска
CREATE INDEX IF NOT EXISTS respect_kb_fts_idx
    ON ai.respect_kb USING GIN (fts);

-- Индекс по section_path — для фильтра по разделам (на v1.6.1+)
CREATE INDEX IF NOT EXISTS respect_kb_section_idx
    ON ai.respect_kb USING GIN (section_path);

-- Индекс по updated_at — для отчётов "что изменилось"
CREATE INDEX IF NOT EXISTS respect_kb_updated_idx
    ON ai.respect_kb (updated_at DESC);

-- ----------------------------------------------------------------------------
-- 2. Кеш распарсенных attachments по sha256
--    Один и тот же файл может быть прикреплён к нескольким карточкам —
--    парсим только один раз. Файл идентифицируется по sha256 содержимого.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ai.respect_kb_attachments (
    sha256          TEXT          PRIMARY KEY,
    url             TEXT          NOT NULL,        -- одна из ссылок (любая, файл идентичен)
    file_type       TEXT          NOT NULL,        -- pdf|docx|txt|md|html|video|audio|image|other
    file_name       TEXT,                          -- имя из последней карточки где встречался
    file_size       BIGINT,                        -- байты (NULL если неизвестно)
    parsed_text     TEXT,                          -- NULL если не парсится / парсинг провалился
    parse_error     TEXT,                          -- последняя ошибка парсинга (для диагностики)
    parsed_at       TIMESTAMPTZ   NOT NULL DEFAULT now()
);

-- Индекс для GC
CREATE INDEX IF NOT EXISTS respect_kb_attachments_parsed_idx
    ON ai.respect_kb_attachments (parsed_at);

-- ----------------------------------------------------------------------------
-- 3. Связь контент <-> attachment (M2M)
--    При удалении карточки — связи удаляются каскадно.
--    Сами файлы в respect_kb_attachments чистятся отдельным GC.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ai.respect_kb_content_attachments (
    content_id      BIGINT NOT NULL
                    REFERENCES ai.respect_kb(content_id) ON DELETE CASCADE,
    sha256          TEXT   NOT NULL
                    REFERENCES ai.respect_kb_attachments(sha256),
    -- сохраняем url+name из конкретной карточки чтобы юзеру отдать ту ссылку,
    -- которая была привязана к этой карточке (имя/URL могут отличаться от того,
    -- что записано в respect_kb_attachments.url)
    display_url     TEXT NOT NULL,
    display_name    TEXT NOT NULL,
    PRIMARY KEY (content_id, sha256)
);

CREATE INDEX IF NOT EXISTS respect_kb_content_attachments_sha_idx
    ON ai.respect_kb_content_attachments(sha256);

-- ----------------------------------------------------------------------------
-- 4. Лог запусков sync (для мониторинга и диагностики)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ai.respect_kb_sync_log (
    id               BIGSERIAL    PRIMARY KEY,
    started_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    finished_at      TIMESTAMPTZ,
    status           TEXT         NOT NULL DEFAULT 'running',  -- running|ok|error|skipped_lock
    rows_upserted    INTEGER      NOT NULL DEFAULT 0,
    rows_deleted     INTEGER      NOT NULL DEFAULT 0,
    attachments_new  INTEGER      NOT NULL DEFAULT 0,           -- скачано/распарсено новых файлов
    attachments_cached INTEGER    NOT NULL DEFAULT 0,           -- найдено в кеше по sha256
    error_message    TEXT
);

CREATE INDEX IF NOT EXISTS respect_kb_sync_log_started_idx
    ON ai.respect_kb_sync_log(started_at DESC);

-- ----------------------------------------------------------------------------
-- 5. Комментарии для документации
-- ----------------------------------------------------------------------------
COMMENT ON TABLE  ai.respect_kb                       IS 'v1.6.0: синхронизированная копия КБ Респект.Чата для FTS-поиска';
COMMENT ON COLUMN ai.respect_kb.content_id            IS 'ID записи на стороне Респект.Чата (источник истины)';
COMMENT ON COLUMN ai.respect_kb.indexable_text        IS 'body_plain + тексты parseable attachments, попадает в FTS';
COMMENT ON COLUMN ai.respect_kb.synced_at             IS 'Когда последний раз синхронизировали эту запись';

COMMENT ON TABLE  ai.respect_kb_attachments           IS 'v1.6.0: кеш распарсенных файлов по sha256';
COMMENT ON COLUMN ai.respect_kb_attachments.parsed_text IS 'Извлечённый текст. NULL если file_type не парсится или парсинг провалился';

COMMENT ON TABLE  ai.respect_kb_content_attachments   IS 'v1.6.0: связь контент<->файл (M2M, один файл может быть в нескольких карточках)';

COMMENT ON TABLE  ai.respect_kb_sync_log              IS 'v1.6.0: лог запусков синхронизации с Респект.Чатом';

COMMIT;

-- =============================================================================
-- Sanity-checks (выполнятся отдельно от транзакции)
-- =============================================================================
\echo ''
\echo '=== Созданные таблицы ==='
SELECT tablename FROM pg_tables WHERE schemaname='ai' AND tablename LIKE 'respect_kb%' ORDER BY tablename;

\echo ''
\echo '=== Размеры (должны быть 0) ==='
SELECT 'respect_kb' AS tbl, count(*) FROM ai.respect_kb
UNION ALL SELECT 'respect_kb_attachments', count(*) FROM ai.respect_kb_attachments
UNION ALL SELECT 'respect_kb_content_attachments', count(*) FROM ai.respect_kb_content_attachments
UNION ALL SELECT 'respect_kb_sync_log', count(*) FROM ai.respect_kb_sync_log;
