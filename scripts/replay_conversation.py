"""Read SQLite chat_messages and pretty-print one conversation.

    python scripts/replay_conversation.py --conv conv-sim
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from sqlalchemy import select

from eidolon_agent.config import load_settings
from eidolon_agent.persistence import create_engine, create_session_factory
from eidolon_agent.persistence.models import ChatMessageRow, TurnRow


async def main(conv_id: str) -> None:
    settings = load_settings()
    engine = create_engine(settings.sqlite)
    sf = create_session_factory(engine)
    async with sf() as s:
        rows = (
            await s.execute(
                select(ChatMessageRow, TurnRow)
                .join(TurnRow, ChatMessageRow.turn_id == TurnRow.id)
                .where(TurnRow.conversation_id == conv_id)
                .order_by(ChatMessageRow.created_at)
            )
        ).all()
    if not rows:
        print(f"no messages for {conv_id} in {settings.sqlite.path}")
        return
    for msg, turn in rows:
        ts = msg.created_at.isoformat(timespec="seconds")
        print(f"[{ts}] {msg.role:>9}: {msg.content}")
    await engine.dispose()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--conv", required=True)
    asyncio.run(main(ap.parse_args().conv))
