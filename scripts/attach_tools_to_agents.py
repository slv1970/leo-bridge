"""
Прикрепить все calendar_* tools ко всем существующим Letta-агентам.

Идемпотентно: если tool уже привязан — пропускаем.
"""
from __future__ import annotations

import os
import sys

import httpx


LETTA_URL = os.environ["LETTA_URL"]
LETTA_PASS = os.environ["LETTA_SERVER_PASSWORD"]


def main() -> None:
    headers = {"Authorization": f"Bearer {LETTA_PASS}"}

    # 1. Все calendar tools
    r = httpx.get(f"{LETTA_URL}/v1/tools/", headers=headers, timeout=30)
    r.raise_for_status()
    cal_tools = [t for t in r.json() if t["name"].startswith("calendar_") or t["name"] in ("internet_search", "leo_create_file") or t["name"].startswith("kb_") or t["name"].startswith("respect_")]
    if not cal_tools:
        print("Нет calendar_* tools. Запустите сначала register_tools.py")
        sys.exit(1)
    print(f"Календарных tools: {len(cal_tools)}")

    # 2. Все агенты
    r = httpx.get(f"{LETTA_URL}/v1/agents/", headers=headers, timeout=30)
    r.raise_for_status()
    agents = r.json()
    print(f"Агентов: {len(agents)}")

    # 3. Привязываем
    for agent in agents:
        agent_id = agent["id"]
        existing_tool_ids = {t["id"] for t in agent.get("tools", [])}
        added = 0
        for tool in cal_tools:
            if tool["id"] in existing_tool_ids:
                continue
            # API Letta 0.16: PATCH /v1/agents/{id}/tools/attach/{tool_id}
            r = httpx.patch(
                f"{LETTA_URL}/v1/agents/{agent_id}/tools/attach/{tool['id']}",
                headers=headers,
                timeout=30,
            )
            if r.status_code >= 400:
                # Fallback — POST на той же ручке (некоторые версии Letta)
                r2 = httpx.post(
                    f"{LETTA_URL}/v1/agents/{agent_id}/tools/attach/{tool['id']}",
                    headers=headers,
                    timeout=30,
                )
                if r2.status_code >= 400:
                    print(
                        f"  FAILED {tool['name']:25s} -> {agent_id[:20]}: "
                        f"PATCH={r.status_code} POST={r2.status_code} "
                        f"body={r.text[:200]}"
                    )
                    continue
            added += 1
        if added:
            print(f"  {agent_id[:25]}: добавлено {added} tools")
        else:
            print(f"  {agent_id[:25]}: уже всё привязано")


if __name__ == "__main__":
    main()

