#!/usr/bin/env python3
"""
v1.6.0 patcher: добавляет в /opt/ai/bridge/app/internal_api.py:
- импорт RespectDBClient
- класс state получает поле respect_client
- lifespan создаёт/закрывает RespectDBClient
- модель RespectKbSearchReq
- endpoint POST /respect_kb/search

Идемпотентен: проверяет наличие маркеров перед вставкой.
Делает backup в .bak-pre-v160 перед правкой.

Запуск:
    sudo -u ai /opt/ai/bridge/venv/bin/python /opt/ai/bridge/scripts/v160_patch_internal_api.py
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

TARGET = Path("/opt/ai/bridge/app/internal_api.py")
BACKUP = Path("/opt/ai/bridge/app/internal_api.py.bak-pre-v160")


# -----------------------------------------------------------------------------
# Маркеры — фрагменты исходного файла, относительно которых вставляем правки.
# Если маркер не найден — патчер прерывается (значит файл уже изменён или не тот).
# -----------------------------------------------------------------------------

MARKER_IMPORT = "from app.timezone_resolver import TimezoneResolver"
INSERT_AFTER_IMPORT = """
# v1.6.0: интеграция с КБ Респект.Чата
from app.respect_db import RespectDBClient
from app.respect_kb_search import respect_kb_search as _respect_kb_search_impl
"""

MARKER_STATE_CLASS = "    matrix_send: Any = None  # callback из bridge для отправки в комнату"
INSERT_AFTER_STATE = """    respect_client: RespectDBClient | None = None  # v1.6.0: КБ Респект.Чата"""

MARKER_LIFESPAN_END = "    log.info(\"Internal API started\")"
INSERT_BEFORE_LIFESPAN_END = """    # v1.6.0: КБ Респект.Чата (опционально — если RESPECT_DATABASE_URL задан)
    if os.environ.get("RESPECT_DATABASE_URL"):
        try:
            state.respect_client = RespectDBClient.from_env()
            await state.respect_client.__aenter__()
            log.info("Respect.Chat KB client initialized")
        except Exception as e:
            log.warning("Respect.Chat KB client init failed: %s", e)
            state.respect_client = None
    else:
        log.info("RESPECT_DATABASE_URL not set, /respect_kb/search disabled")

"""

MARKER_LIFESPAN_SHUTDOWN = "    if state.tz_resolver:\n        await state.tz_resolver.__aexit__(None, None, None)"
INSERT_AFTER_SHUTDOWN_TZ = """    if state.respect_client:
        try:
            await state.respect_client.__aexit__(None, None, None)
        except Exception:
            pass
"""

# В конец файла (перед def serve()) — модель + endpoint
MARKER_BEFORE_SERVE = "def serve() -> None:"
INSERT_BEFORE_SERVE = '''
# =============================================================================
# v1.6.0: КБ Респект.Чата
# =============================================================================
class RespectKbSearchReq(BaseModel):
    query: str = Field(..., description="Текст поискового запроса")
    matrix_user_id: str = Field(..., description="MXID пользователя для проверки прав")
    limit: int = Field(5, ge=1, le=20)


@app.post("/respect_kb/search")
async def respect_kb_search_endpoint(
    req: RespectKbSearchReq, _: None = Depends(check_auth)
) -> dict[str, Any]:
    """v1.6.0: поиск в КБ Респект.Чата с учётом прав пользователя.

    ACL применяется на стороне Респект.Чата через PG-функцию
    kb_get_accessible_content_ids(matrix_user_id).
    FTS-поиск делается локально по синхронизированной копии (ai.respect_kb).
    """
    if state.respect_client is None or state.pg_pool is None:
        raise HTTPException(
            status_code=503,
            detail="Respect.Chat KB integration is not configured (RESPECT_DATABASE_URL missing)",
        )
    return await _respect_kb_search_impl(
        leo_pool=state.pg_pool,
        respect_client=state.respect_client,
        query=req.query,
        matrix_user_id=req.matrix_user_id,
        limit=req.limit,
    )


'''


def patch() -> int:
    if not TARGET.exists():
        print(f"ERROR: {TARGET} not found", file=sys.stderr)
        return 2

    text = TARGET.read_text(encoding="utf-8")

    # Идемпотентность: если маркер уже вставлен — выходим без изменений.
    if "from app.respect_db import RespectDBClient" in text:
        print(f"Already patched: {TARGET}")
        return 0

    # Backup
    if not BACKUP.exists():
        shutil.copy2(TARGET, BACKUP)
        print(f"Backup: {BACKUP}")
    else:
        print(f"Backup already exists: {BACKUP} (skip)")

    # 1. Import after timezone_resolver
    if MARKER_IMPORT not in text:
        print("ERROR: import marker not found", file=sys.stderr)
        return 3
    text = text.replace(
        MARKER_IMPORT,
        MARKER_IMPORT + INSERT_AFTER_IMPORT,
        1,
    )

    # 2. State.respect_client field
    if MARKER_STATE_CLASS not in text:
        print("ERROR: State class marker not found", file=sys.stderr)
        return 3
    text = text.replace(
        MARKER_STATE_CLASS,
        MARKER_STATE_CLASS + "\n" + INSERT_AFTER_STATE,
        1,
    )

    # 3. Lifespan: init Respect client before "Internal API started"
    if MARKER_LIFESPAN_END not in text:
        print("ERROR: lifespan-start marker not found", file=sys.stderr)
        return 3
    text = text.replace(
        MARKER_LIFESPAN_END,
        INSERT_BEFORE_LIFESPAN_END + MARKER_LIFESPAN_END,
        1,
    )

    # 4. Lifespan: close Respect client on shutdown
    if MARKER_LIFESPAN_SHUTDOWN not in text:
        print("ERROR: lifespan-shutdown marker not found", file=sys.stderr)
        return 3
    text = text.replace(
        MARKER_LIFESPAN_SHUTDOWN,
        MARKER_LIFESPAN_SHUTDOWN + "\n" + INSERT_AFTER_SHUTDOWN_TZ,
        1,
    )

    # 5. Endpoint before def serve()
    if MARKER_BEFORE_SERVE not in text:
        print("ERROR: serve() marker not found", file=sys.stderr)
        return 3
    text = text.replace(
        MARKER_BEFORE_SERVE,
        INSERT_BEFORE_SERVE + MARKER_BEFORE_SERVE,
        1,
    )

    TARGET.write_text(text, encoding="utf-8")
    print(f"Patched: {TARGET}")
    print("Run: sudo systemctl restart bridge-internal-api")
    return 0


if __name__ == "__main__":
    sys.exit(patch())
