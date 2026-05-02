#!/usr/bin/env python3
"""
v1.6.0 patcher: расширяет _is_our_tool в /opt/ai/bridge/app/letta_client.py
чтобы распознавать tools с префиксом respect_ как «свои» — это нужно для:
- автопривязки к новым агентам в create_agent
- фильтрации в _get_calendar_tool_ids

Запуск:
    sudo -u ai /opt/ai/bridge/venv/bin/python /opt/ai/bridge/scripts/v160_patch_letta_client.py
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

TARGET = Path("/opt/ai/bridge/app/letta_client.py")
BACKUP = Path("/opt/ai/bridge/app/letta_client.py.bak-pre-v160")

OLD = '''    @classmethod
    def _is_our_tool(cls, tool_name: str) -> bool:
        """v0.8.10: tool считается «нашим» если:
        - имя начинается с calendar_  (календарные tools)
        - имя начинается с kb_  (KB tools: search_corporate/personal, list/info/delete_personal)
        - явно перечислено в _OUR_TOOL_NAMES (internet_search и др.)
        """
        return (
            tool_name.startswith("calendar_")
            or tool_name.startswith("kb_")
            or tool_name in cls._OUR_TOOL_NAMES
        )'''

NEW = '''    @classmethod
    def _is_our_tool(cls, tool_name: str) -> bool:
        """v0.8.10 / v1.6.0: tool считается «нашим» если:
        - имя начинается с calendar_  (календарные tools)
        - имя начинается с kb_  (KB tools: search_corporate/personal, list/info/delete_personal)
        - имя начинается с respect_  (v1.6.0: КБ Респект.Чата)
        - явно перечислено в _OUR_TOOL_NAMES (internet_search и др.)
        """
        return (
            tool_name.startswith("calendar_")
            or tool_name.startswith("kb_")
            or tool_name.startswith("respect_")
            or tool_name in cls._OUR_TOOL_NAMES
        )'''


def patch() -> int:
    if not TARGET.exists():
        print(f"ERROR: {TARGET} not found", file=sys.stderr)
        return 2

    text = TARGET.read_text(encoding="utf-8")

    if "tool_name.startswith(\"respect_\")" in text:
        print(f"Already patched: {TARGET}")
        return 0

    if OLD not in text:
        print("ERROR: _is_our_tool marker not found in expected form", file=sys.stderr)
        print("       Возможно, файл уже модифицирован — проверь руками.", file=sys.stderr)
        return 3

    if not BACKUP.exists():
        shutil.copy2(TARGET, BACKUP)
        print(f"Backup: {BACKUP}")
    else:
        print(f"Backup already exists: {BACKUP}")

    text = text.replace(OLD, NEW, 1)
    TARGET.write_text(text, encoding="utf-8")
    print(f"Patched: {TARGET}")
    print("Run: sudo systemctl restart matrix-letta-bridge")
    return 0


if __name__ == "__main__":
    sys.exit(patch())
