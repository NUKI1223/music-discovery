import sqlite3

import pytest

from discovery.llm import BudgetLedger, LLMError


def test_without_account_behaviour_unchanged(tmp_path):
    ledger = BudgetLedger(tmp_path / 'b.sqlite')
    ledger.reserve(0.04, 0.10)
    ledger.reserve(0.04, 0.10)
    with pytest.raises(LLMError) as error:
        ledger.reserve(0.04, 0.10)
    assert error.value.code == 'DAILY_BUDGET'
    assert 'форму' not in str(error.value)


def test_account_limit_blocks_only_that_account(tmp_path):
    ledger = BudgetLedger(tmp_path / 'b.sqlite')
    ledger.reserve(0.03, 0.10, 'a', 0.05)
    with pytest.raises(LLMError) as error:
        ledger.reserve(0.03, 0.10, 'a', 0.05)
    assert error.value.code == 'ACCOUNT_DAILY_BUDGET'
    assert 'Ваш дневной лимит помощника исчерпан' in str(error.value)
    ledger.reserve(0.03, 0.10, 'b', 0.05)
    # Shared limit still applies to everyone.
    with pytest.raises(LLMError) as error:
        ledger.reserve(0.05, 0.10, 'c', 0.05)
    assert error.value.code == 'DAILY_BUDGET'


def test_account_limit_needs_both_arguments(tmp_path):
    ledger = BudgetLedger(tmp_path / 'b.sqlite')
    ledger.reserve(0.04, 0.10, 'a', None)
    ledger.reserve(0.04, 0.10, None, 0.01)


def test_old_database_is_migrated(tmp_path):
    path = tmp_path / 'old.sqlite'
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE llm_reservations (id TEXT PRIMARY KEY, day TEXT NOT NULL, '
                 'amount REAL NOT NULL, settled INTEGER NOT NULL DEFAULT 0)')
    conn.execute("INSERT INTO llm_reservations(id,day,amount) VALUES('x','2000-01-01',0.01)")
    conn.commit()
    conn.close()
    ledger = BudgetLedger(path)
    BudgetLedger(path)  # idempotent
    ledger.reserve(0.01, 0.10, 'a', 0.05)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT account_id FROM llm_reservations WHERE id='x'").fetchone()[0] is None


def test_locked_database_gives_busy_error(tmp_path, monkeypatch):
    ledger = BudgetLedger(tmp_path / 'b.sqlite')
    blocker = sqlite3.connect(ledger.path, isolation_level=None)
    blocker.execute('BEGIN IMMEDIATE')
    real = sqlite3.connect
    monkeypatch.setattr(sqlite3, 'connect', lambda path, timeout=5, **kw: real(path, timeout=0.05, **kw))
    try:
        with pytest.raises(LLMError) as error:
            ledger.reserve(0.01, 0.10)
    finally:
        blocker.rollback()
        blocker.close()
    assert error.value.code == 'BUDGET_BUSY'
    assert str(error.value) == 'Помощник занят, повторите позже.'
