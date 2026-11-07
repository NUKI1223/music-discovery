"""Persistent conservative cost reservations, including unknown timeout costs."""

import sqlite3
import time
import uuid
from collections.abc import Callable
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .errors import LLMError


class BudgetLedger:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path == ':memory:':
            raise ValueError('Use a file-backed ledger so limits survive restarts.')
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS llm_reservations '
                         '(id TEXT PRIMARY KEY, day TEXT NOT NULL, amount REAL NOT NULL, '
                         'settled INTEGER NOT NULL DEFAULT 0, account_id TEXT)')
            if 'account_id' not in {row[1] for row in conn.execute('PRAGMA table_info(llm_reservations)')}:
                conn.execute('ALTER TABLE llm_reservations ADD COLUMN account_id TEXT')
            conn.execute('CREATE TABLE IF NOT EXISTS llm_budget_state '
                         '(key TEXT PRIMARY KEY, value TEXT NOT NULL)')

    @contextmanager
    def _connect(self, timeout: float = 5):
        # sqlite3's own context manager commits but never closes the connection.
        with closing(sqlite3.connect(self.path, timeout=timeout)) as conn:
            with conn:
                yield conn

    def reserve(self, amount: float, daily_limit: float, account_id: str | None = None,
                account_daily_limit: float | None = None) -> str:
        today = datetime.now(timezone.utc).date().isoformat()
        reservation = uuid.uuid4().hex
        try:
            with self._connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                if conn.execute("SELECT 1 FROM llm_budget_state WHERE key='suspended'").fetchone():
                    raise LLMError('BUDGET_SUSPENDED', 'LLM остановлена после расхождения тарифов; нужна проверка бюджета.')
                spent = conn.execute('SELECT coalesce(sum(amount),0) FROM llm_reservations '
                                     'WHERE day=?', (today,)).fetchone()[0]
                if spent + amount > daily_limit + 1e-12:
                    raise LLMError('DAILY_BUDGET', 'Дневной бюджет LLM исчерпан. Повторите позже.')
                if account_id is not None and account_daily_limit is not None:
                    own = conn.execute('SELECT coalesce(sum(amount),0) FROM llm_reservations '
                                       'WHERE day=? AND account_id=?', (today, account_id)).fetchone()[0]
                    if own + amount > account_daily_limit + 1e-12:
                        raise LLMError('ACCOUNT_DAILY_BUDGET',
                                       'Ваш дневной лимит помощника исчерпан. Он обновится в 00:00 UTC.')
                conn.execute('INSERT INTO llm_reservations(id,day,amount,account_id) VALUES(?,?,?,?)',
                             (reservation, today, amount, account_id))
        except sqlite3.OperationalError:
            raise LLMError('BUDGET_BUSY', 'Помощник занят, повторите позже.') from None
        return reservation

    def suspend(self) -> None:
        with self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO llm_budget_state(key,value) VALUES('suspended','rate_mismatch')")

    def settle(self, reservation: str, actual: float) -> None:
        with self._connect() as conn:
            conn.execute('UPDATE llm_reservations SET amount=?,settled=1 WHERE id=?',
                         (actual, reservation))

    def total(self) -> float:
        with self._connect() as conn:
            return conn.execute('SELECT coalesce(sum(amount),0) FROM llm_reservations').fetchone()[0]


@dataclass
class RunBudget:
    max_attempts: int = 4
    max_turns: int = 3
    max_retries: int = 1
    deadline_seconds: float = 45
    clock: Callable[[], float] = time.monotonic
    attempts: int = 0
    turns: int = 0
    retries: int = 0
    reserved_cost: float = 0
    reserved_tokens: int = 0
    started: float = field(init=False)

    def __post_init__(self):
        self.started = self.clock()

    def remaining(self) -> float:
        remaining = self.deadline_seconds - (self.clock() - self.started)
        if remaining <= 0:
            raise LLMError('DEADLINE', 'Время ожидания LLM истекло. Повторите позже.')
        return remaining

    def turn(self):
        self.remaining()
        if self.turns >= self.max_turns:
            raise LLMError('TURN_LIMIT', 'Достигнут лимит шагов LLM.')
        self.turns += 1

    def attempt(self, amount: float, tokens: int, request_limit: float, token_limit: int):
        self.remaining()
        if self.attempts >= self.max_attempts:
            raise LLMError('ATTEMPT_LIMIT', 'Достигнут лимит обращений к LLM.')
        if self.reserved_cost + amount > request_limit + 1e-12:
            raise LLMError('REQUEST_BUDGET', 'Бюджет запроса LLM исчерпан.')
        if self.reserved_tokens + tokens > token_limit:
            raise LLMError('TOKEN_BUDGET', 'Лимит токенов запроса исчерпан.')
        self.attempts += 1
        self.reserved_cost += amount
        self.reserved_tokens += tokens
