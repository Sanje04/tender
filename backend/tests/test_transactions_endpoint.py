"""
Tests for GET /api/transactions (Phase 4) -- the read-only endpoint used by
the frontend's transactions panel display only (separate from the agent's
tool-calling path, tested in test_agent_tools.py). Hermetic like test_chat.py:
TestClient(main.app) is instantiated without `with`, so startup's
db.ensure_indexes() never runs against a real MongoDB, and db functions are
monkeypatched so no real MongoDB is needed.
"""

import pytest
from fastapi.testclient import TestClient

import db
import main

client = TestClient(main.app)


def test_get_transactions_returns_accounts_and_transactions(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_list_accounts() -> list[dict[str, object]]:
        return [{"id": "checking", "name": "Checking", "type": "checking", "current_balance": 100.0}]

    async def fake_search_transactions(**kwargs: object) -> list[dict[str, object]]:
        return [
            {
                "id": "1",
                "account_id": "checking",
                "account_name": "Checking",
                "account_type": "checking",
                "date": "2026-08-01T00:00:00+00:00",
                "amount": -50.0,
                "merchant": "Test Merchant",
                "description": "test transaction",
                "category": "Groceries",
                "running_balance": 50.0,
            }
        ]

    monkeypatch.setattr(db, "list_accounts", fake_list_accounts)
    monkeypatch.setattr(db, "search_transactions", fake_search_transactions)

    response = client.get("/api/transactions")

    assert response.status_code == 200
    body = response.json()
    assert len(body["accounts"]) == 1
    assert len(body["transactions"]) == 1
    assert body["transactions"][0]["merchant"] == "Test Merchant"


def test_get_transactions_returns_503_on_db_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def failing_list_accounts() -> list[dict[str, object]]:
        raise Exception("mongo unreachable")

    monkeypatch.setattr(db, "list_accounts", failing_list_accounts)

    response = client.get("/api/transactions")

    assert response.status_code == 503
    assert isinstance(response.json()["error"], str)


# --- POST /api/transactions/import: the IMPORT_ENABLED gate -----------------
#
# Only the gate is covered here. The CSV parsing and full-replace semantics the
# endpoint delegates to are already tested against db.import_transactions in
# test_import_transactions.py, so these two send a well-formed multipart body
# deliberately: the request has to survive FastAPI's form validation for the
# gate inside the handler to be what decides the outcome.

_IMPORT_FILES = {"file": ("statement.csv", "Transaction Type,Date Posted\n", "text/csv")}
_IMPORT_FORM = {"account_name": "Checking", "account_type": "checking", "opening_balance": "0"}


def test_import_is_served_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_import(*args: object) -> dict[str, object]:
        return {"imported_count": 1, "accounts": []}

    monkeypatch.setattr(main, "IMPORT_ENABLED", True)
    monkeypatch.setattr(db, "import_transactions", fake_import)

    response = client.post("/api/transactions/import", files=_IMPORT_FILES, data=_IMPORT_FORM)

    assert response.status_code == 200
    assert response.json()["imported_count"] == 1


def test_import_is_404_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deployment with the gate closed must look like it has no such route.

    db.import_transactions is replaced with a function that fails the test if it
    runs: a 404 that still wiped the database would pass a status-code-only
    assertion, and that is the whole failure this gate exists to prevent.
    """

    async def must_not_run(*args: object) -> dict[str, object]:
        raise AssertionError("import_transactions ran despite IMPORT_ENABLED=False")

    monkeypatch.setattr(main, "IMPORT_ENABLED", False)
    monkeypatch.setattr(db, "import_transactions", must_not_run)

    response = client.post("/api/transactions/import", files=_IMPORT_FILES, data=_IMPORT_FORM)

    assert response.status_code == 404
    assert isinstance(response.json()["error"], str)
