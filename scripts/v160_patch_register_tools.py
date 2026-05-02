#!/usr/bin/env python3
"""
v1.6.0 patcher: добавляет в /opt/ai/bridge/scripts/register_tools.py:
- функцию respect_kb_search (Letta-tool)
- запись в TOOLS = [...]

После применения нужно запустить сам register_tools.py — это зальёт
новый tool в Letta. Затем attach_tools_to_agents.py — привяжет к 43 агентам.

Запуск:
    sudo -u ai /opt/ai/bridge/venv/bin/python /opt/ai/bridge/scripts/v160_patch_register_tools.py
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

TARGET = Path("/opt/ai/bridge/scripts/register_tools.py")
BACKUP = Path("/opt/ai/bridge/scripts/register_tools.py.bak-pre-v160")


# Маркер: вставляем новую функцию ПЕРЕД блоком "============= Регистрация в Letta"
MARKER_BEFORE_REGISTRATION = """# ============================================================
# Регистрация в Letta
# ============================================================"""

NEW_FUNCTION = '''
def respect_kb_search(
    query: str,
    matrix_user_id: str,
    limit: int = 5,
) -> str:
    """
    Поиск в КОРПОРАТИВНОЙ базе знаний Респект.Чата с учётом прав пользователя.

    Это основная корпоративная KB компании: методические материалы, инфоповоды,
    экспертные материалы, сравнения с конкурентами, обучающие материалы.
    Все материалы структурированы по разделам и имеют разные уровни доступа —
    каждый пользователь видит только то, к чему у него есть доступ в Респект.Чате.

    К материалам могут быть прикреплены файлы (PDF, DOCX), видео и аудио —
    Leo вернёт прямые ссылки на них.

    Args:
        query: Поисковый запрос на русском языке. Будь конкретным:
               "сравнение Гарант Консультант", "инструкция по командировкам",
               "методичка для новых сотрудников".
        matrix_user_id: MXID пользователя — ОБЯЗАТЕЛЬНО возьми из [from=...]
                        в контексте сообщения. Без него поиск работать не будет
                        (нужен для проверки прав доступа).
        limit: Сколько материалов вернуть (1-20). По умолчанию 5.

    Returns:
        Текстовая сводка найденных материалов с разделами, фрагментами,
        ссылками на прикреплённые файлы. Либо сообщение что ничего не найдено,
        либо что у пользователя нет доступа.

    Когда использовать:
        - Вопросы про корпоративные материалы Респект.Чата
        - "Что у нас есть про X?", "Где почитать про Y?", "Найди материалы по Z"
        - Сравнения с конкурентами (Гарант, КонсультантПлюс, Актион и др.)
        - Методические материалы, инфоповоды, экспертные материалы
        - Обучающие материалы для сотрудников и пользователей

    Когда НЕ использовать:
        - Личные заметки пользователя (kb_search_personal)
        - Старая корпоративная KB на pgvector (kb_search_corporate)
        - Свежие новости / актуальные данные из интернета (internet_search)
        - Календарь (calendar_*)
    """
    import os
    import httpx

    if not matrix_user_id:
        return "Не могу искать в корпоративной KB — нужен MXID пользователя (matrix_user_id)."

    body = {
        "query": query,
        "matrix_user_id": matrix_user_id,
        "limit": max(1, min(20, limit)),
    }
    try:
        r = httpx.post(
            "http://127.0.0.1:8284/respect_kb/search",
            headers={"X-Internal-Token": os.environ["BRIDGE_INTERNAL_TOKEN"]},
            json=body,
            timeout=30,
        )
    except Exception as e:
        return f"Ошибка соединения с КБ Респект.Чата: {e}"

    if r.status_code == 503:
        return "Корпоративная KB Респект.Чата сейчас не подключена. Обратись к администратору."
    if r.status_code >= 400:
        return f"Ошибка поиска: HTTP {r.status_code}: {r.text[:300]}"

    data = r.json()
    return data.get("formatted") or "По запросу ничего не найдено."


'''

# Маркер для добавления в TOOLS — последний tool в списке
MARKER_TOOLS_LAST = "    leo_create_file,\n]"
NEW_TOOLS_LIST = "    leo_create_file,\n    respect_kb_search,\n]"


def patch() -> int:
    if not TARGET.exists():
        print(f"ERROR: {TARGET} not found", file=sys.stderr)
        return 2

    text = TARGET.read_text(encoding="utf-8")

    if "def respect_kb_search(" in text:
        print(f"Already patched: {TARGET}")
        return 0

    if MARKER_BEFORE_REGISTRATION not in text:
        print("ERROR: registration-section marker not found", file=sys.stderr)
        return 3
    if MARKER_TOOLS_LAST not in text:
        print("ERROR: TOOLS-list marker not found", file=sys.stderr)
        return 3

    if not BACKUP.exists():
        shutil.copy2(TARGET, BACKUP)
        print(f"Backup: {BACKUP}")
    else:
        print(f"Backup already exists: {BACKUP}")

    text = text.replace(
        MARKER_BEFORE_REGISTRATION,
        NEW_FUNCTION + MARKER_BEFORE_REGISTRATION,
        1,
    )
    text = text.replace(MARKER_TOOLS_LAST, NEW_TOOLS_LIST, 1)

    TARGET.write_text(text, encoding="utf-8")
    print(f"Patched: {TARGET}")
    print("Next: run register_tools.py to push new tool to Letta")
    return 0


if __name__ == "__main__":
    sys.exit(patch())
