"""
Tests for db.get_recent_history() (multi-turn context, specs.md Phase 7).

FakeConversations is a minimal stand-in for the `conversations` collection --
only what get_recent_history() calls: an async find_one() that honors a
{"messages": {"$slice": -N}} projection. Not a general MongoDB emulator, same
spirit as test_db_transactions.py's FakeCollection.

Async db.py functions are run via asyncio.run() inside plain `def` tests
rather than pulling in pytest-asyncio, matching this project's minimal test
dependencies (see requirements-dev.txt).
"""

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest

import db

_TS = datetime.now(timezone.utc)


class FakeConversations:
    def __init__(self, doc: dict[str, Any] | None) -> None:
        self._doc = doc

    async def find_one(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        if self._doc is None:
            return None
        doc = dict(self._doc)
        projection = kwargs.get("projection") or {}
        slice_n = projection.get("messages", {}).get("$slice")
        if slice_n is not None:
            doc["messages"] = doc["messages"][slice_n:]
        return doc


def _msg(i: int) -> dict[str, Any]:
    return {"role": "user" if i % 3 == 0 else "assistant", "content": f"msg{i}", "timestamp": _TS}


def test_get_recent_history_returns_last_n_turns_oldest_first(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = {"messages": [_msg(i) for i in range(12)]}
    monkeypatch.setattr(db, "conversations", FakeConversations(doc))

    result = asyncio.run(db.get_recent_history(max_turns=5))

    assert len(result) == 10
    assert result[0] == {"role": "user", "content": "msg2"}
    assert result[-1] == {"role": "assistant", "content": "msg11"}
    assert all("timestamp" not in m for m in result)


def test_get_recent_history_no_conversation_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "conversations", FakeConversations(None))

    assert asyncio.run(db.get_recent_history()) == []


def test_get_recent_history_non_positive_max_turns_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    # Guards against the Mongo/Python footgun where a 0 slice limit means
    # "the whole array", not "nothing" -- see get_recent_history's docstring.
    doc = {"messages": [_msg(i) for i in range(4)]}
    monkeypatch.setattr(db, "conversations", FakeConversations(doc))

    assert asyncio.run(db.get_recent_history(max_turns=0)) == []
