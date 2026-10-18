from __future__ import annotations

import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from typing import Any

import pytest

from litforge.analysis.gateway import ModelGateway
from litforge.config import Config, ModelProfileConfig, Paths
from litforge.llm import EvidenceItem, EvidencePacket, LlmError, ModelReply, ModelUsage
from litforge.llm.budget import BudgetExceeded, Pricing
from litforge.storage import recovery
from litforge.storage.db import connect, now

MESSAGES = [{"role": "user", "content": "hello"}]
SCHEMA = {"type": "object"}
PACKET = EvidencePacket(items=[EvidenceItem(id="e", paper_id="p", text="quote", page=1)])


@pytest.fixture()
def config() -> Config:
    cfg = Config()
    cfg.models.policy = "hybrid"
    cfg.privacy.allow_cloud = True
    cfg.models.profiles["remote"] = ModelProfileConfig(
        provider="openai_compatible", model="remote", remote=True,
        pricing=Pricing(input_per_million_usd=1, output_per_million_usd=2))
    cfg.budget.max_per_call_usd = 1
    cfg.budget.max_per_run_usd = cfg.budget.max_per_month_usd = 0.3
    return cfg


class Provider:
    def __init__(self, conn: sqlite3.Connection, entered: Event | None = None,
                 release: Event | None = None, error: BaseException | None = None,
                 output_tokens: int = 100_000) -> None:
        self.conn, self.entered, self.release, self.error = conn, entered, release, error
        self.output_tokens = output_tokens
        self.calls = 0

    def chat_json(self, messages: list[dict[str, str]], schema: dict[str, Any]) -> ModelReply:
        assert not self.conn.in_transaction, "write transaction held during network call"
        self.calls += 1
        if self.entered:
            self.entered.set()
        if self.release:
            assert self.release.wait(10), "test failed to release slow provider"
        if self.error:
            raise self.error
        return ModelReply(content={}, provider="p", model="m", usage=ModelUsage(output_tokens=self.output_tokens))


@pytest.mark.parametrize("limit", ["run", "month", "month_without_run"])
def test_concurrent_remote_reserves_before_dispatch(conn: sqlite3.Connection, paths: Paths,
                                                   config: Config, limit: str) -> None:
    if limit != "run":
        config.budget.max_per_run_usd = 10
    else:
        config.budget.max_per_month_usd = 10
    run = None if limit == "month_without_run" else "shared"
    entered, release = Event(), Event()

    def first() -> None:
        with connect(paths.db) as worker_conn:
            provider = Provider(worker_conn, entered, release, output_tokens=10_000)
            ModelGateway(worker_conn, config, {"remote": provider}).call(
                "t", PACKET, MESSAGES, SCHEMA, profile="remote", run_id=run, max_output_tokens=100_000)
        worker_conn.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(first)
        try:
            assert entered.wait(5)
            second = Provider(conn)
            gateway = ModelGateway(conn, config, {"remote": second, "local": Provider(conn)})
            with pytest.raises(BudgetExceeded, match="run" if limit == "run" else "month"):
                gateway.call("t", PACKET, MESSAGES, SCHEMA, profile="remote",
                             run_id=run if limit == "run" else "other", max_output_tokens=100_000)
            assert second.calls == 0
            assert gateway.preview("t", PACKET, MESSAGES, profile="remote",
                                   max_output_tokens=100_000).blocked_reason == (
                                       None if limit == "run" else "over budget")
            # A local call still runs while the remote budget is reserved.
            gateway.call("t", PACKET, MESSAGES, SCHEMA, run_id=run)
        finally:
            release.set()
        future.result(timeout=5)
    assert conn.execute("SELECT COUNT(*) FROM model_call_reservations").fetchone()[0] == 0
    assert conn.execute("SELECT SUM(cost_usd) FROM model_calls").fetchone()[0] == pytest.approx(0.02)
    # The unused estimate is available again after reconciliation.
    gateway.call("t", PACKET, MESSAGES, SCHEMA, profile="remote", run_id=run, max_output_tokens=100_000)
    assert conn.execute("SELECT SUM(cost_usd) FROM model_calls").fetchone()[0] == pytest.approx(0.22)


@pytest.mark.parametrize("error", [LlmError("failed"), KeyboardInterrupt()])
def test_failed_call_releases_reservation(conn: sqlite3.Connection, config: Config, error: BaseException) -> None:
    provider = Provider(conn, error=error)
    gateway = ModelGateway(conn, config, {"remote": provider})
    with pytest.raises(type(error)):
        gateway.call("t", PACKET, MESSAGES, SCHEMA, profile="remote", run_id="r", max_output_tokens=100_000)
    assert conn.execute("SELECT COUNT(*) FROM model_call_reservations").fetchone()[0] == 0
    provider.error = None
    gateway.call("t", PACKET, MESSAGES, SCHEMA, profile="remote", run_id="r", max_output_tokens=100_000)
    assert provider.calls == 2


@pytest.mark.parametrize("owner", ["dead", "expired", "live"])
def test_reservation_owner_recovery(conn: sqlite3.Connection, config: Config,
                                   monkeypatch: pytest.MonkeyPatch, owner: str) -> None:
    pid = os.getpid()
    monkeypatch.setattr(recovery, "_alive", lambda value: value == pid)
    conn.execute("INSERT INTO model_call_reservations(id,run_id,cost_usd,created_at,owner_pid,owner_started_at) "
                 "VALUES ('abandoned','r',0.2,?,?,?)",
                 (now(), pid if owner != "dead" else pid + 1,
                  "2000-01-01T00:00:00+00:00" if owner == "expired" else now()))
    gateway = ModelGateway(conn, config, {"remote": Provider(conn)})
    assert gateway.preview("t", PACKET, MESSAGES, profile="remote", max_output_tokens=100_000).blocked_reason == (
        "over budget" if owner == "live" else None)
    if owner == "live":
        with pytest.raises(BudgetExceeded):
            gateway.call("t", PACKET, MESSAGES, SCHEMA, profile="remote", run_id="r", max_output_tokens=100_000)
    else:
        gateway.call("t", PACKET, MESSAGES, SCHEMA, profile="remote", run_id="r", max_output_tokens=100_000)
        assert conn.execute("SELECT COUNT(*) FROM model_call_reservations").fetchone()[0] == 0


def test_reconciliation_rereads_concurrent_actual_spend(conn: sqlite3.Connection, paths: Paths,
                                                      config: Config) -> None:
    entered, release = Event(), Event()
    def first() -> None:
        worker_conn = connect(paths.db)
        try:
            ModelGateway(worker_conn, config, {"remote": Provider(worker_conn, entered, release)}).call(
                "t", PACKET, MESSAGES, SCHEMA, profile="remote", run_id="r", max_output_tokens=1000)
        finally:
            worker_conn.close()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(first)
        try:
            assert entered.wait(5)
            ModelGateway(conn, config, {"remote": Provider(conn)}).call(
                "t", PACKET, MESSAGES, SCHEMA, profile="remote", run_id="r", max_output_tokens=1000)
        finally:
            release.set()
        with pytest.raises(BudgetExceeded):
            future.result(timeout=5)
    assert conn.execute("SELECT COUNT(*) FROM model_call_reservations").fetchone()[0] == 0
    rows = conn.execute("SELECT status,cost_usd FROM model_calls ORDER BY rowid").fetchall()
    assert [row["status"] for row in rows] == ["ok", "over_budget"]
    assert sum(row["cost_usd"] for row in rows) == pytest.approx(0.4)
