#!/usr/bin/env python3
"""
v1.6.0 patcher: расширяет фильтр в attach_tools_to_agents.py
чтобы респект-tools попадали в массовую привязку к существующим агентам.

Запуск:
    sudo -u ai /opt/ai/bridge/venv/bin/python /opt/ai/bridge/scripts/v160_patch_attach_tools.py
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

TARGET = Path("/opt/ai/bridge/scripts/attach_tools_to_agents.py")
BACKUP = Path("/opt/ai/bridge/scripts/attach_tools_to_agents.py.bak-pre-v160")

OLD = '    cal_tools = [t for t in r.json() if t["name"].startswith("calendar_") or t["name"] in ("internet_search", "leo_create_file") or t["name"].startswith("kb_")]'
NEW = '    cal_tools = [t for t in r.json() if t["name"].startswith("calendar_") or t["name"] in ("internet_search", "leo_create_file") or t["name"].startswith("kb_") or t["name"].startswith("respect_")]'


def patch() -> int:
    if not TARGET.exists():
        print(f"ERROR: {TARGET} not found", file=sys.stderr)
        return 2

    text = TARGET.read_text(encoding="utf-8")

    if 'startswith("respect_")' in text:
        print(f"Already patched: {TARGET}")
        return 0

    if OLD not in text:
        print("ERROR: filter line not found in expected form", file=sys.stderr)
        return 3

    if not BACKUP.exists():
        shutil.copy2(TARGET, BACKUP)
        print(f"Backup: {BACKUP}")
    else:
        print(f"Backup already exists: {BACKUP}")

    text = text.replace(OLD, NEW, 1)
    TARGET.write_text(text, encoding="utf-8")
    print(f"Patched: {TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(patch())
