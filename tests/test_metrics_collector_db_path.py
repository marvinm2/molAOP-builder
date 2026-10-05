"""
Regression tests for the request-metrics collector.

monitor_performance used to record through a module-global MetricsCollector
built at import time with the relative path "ke_wp_mapping.db". In production
the working directory (/app) is not writable, so every start logged "Failed to
initialize metrics tables" and every monitored request logged "Failed to store
metric" — while the container already held a collector on DATABASE_PATH.
"""
import logging
import sqlite3
import time
from types import SimpleNamespace

import pytest
from flask import Flask

from src.services import monitoring
from src.services.monitoring import MetricsCollector, monitor_performance


class _SyncThread:
    """Stand-in for threading.Thread that runs the target on start()."""

    def __init__(self, target, args=(), kwargs=None):
        self._target, self._args, self._kwargs = target, args, kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


@pytest.fixture
def sync_threads(monkeypatch):
    monkeypatch.setattr(monitoring.threading, "Thread", _SyncThread)


def _rows(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT endpoint, method, status_code, response_time, client_ip, user_agent"
            " FROM metrics"
        ).fetchall()
    finally:
        conn.close()


def _app_with_collector(collector):
    app = Flask(__name__)
    app.service_container = SimpleNamespace(metrics_collector=collector)

    @app.route("/probe")
    @monitor_performance
    def probe():
        return "ok"

    return app


def test_no_import_time_global_collector():
    assert not hasattr(monitoring, "metrics_collector")


def test_decorator_records_through_app_container(tmp_path, sync_threads):
    db_path = str(tmp_path / "metrics.db")
    app = _app_with_collector(MetricsCollector(db_path))

    resp = app.test_client().get(
        "/probe",
        headers={"User-Agent": "probe-agent", "X-Forwarded-For": "203.0.113.7"},
    )
    assert resp.status_code == 200

    rows = _rows(db_path)
    assert len(rows) == 1
    endpoint, method, status, latency, client_ip, user_agent = rows[0]
    assert (endpoint, method, status) == ("probe", "GET", 200)
    assert latency >= 0
    # Neither the client IP nor the user agent is persisted.
    assert client_ip is None
    assert user_agent is None


def test_decorator_degrades_without_app_context():
    @monitor_performance
    def plain():
        return 42

    assert plain() == 42


def test_decorator_degrades_without_service_container(
    sync_threads, tmp_path, monkeypatch
):
    def _must_not_record(*args, **kwargs):
        raise AssertionError("recorded without a service container")

    monkeypatch.setattr(MetricsCollector, "record_request", _must_not_record)
    monkeypatch.chdir(tmp_path)
    app = Flask(__name__)

    @app.route("/probe")
    @monitor_performance
    def probe():
        return "ok"

    assert app.test_client().get("/probe").status_code == 200
    # Nothing falls back to a relative database in the working directory.
    assert not (tmp_path / "ke_wp_mapping.db").exists()


def test_failing_route_records_500_and_reraises(tmp_path, sync_threads):
    db_path = str(tmp_path / "metrics.db")
    app = Flask(__name__)
    app.service_container = SimpleNamespace(metrics_collector=MetricsCollector(db_path))

    @app.route("/boom")
    @monitor_performance
    def boom():
        raise ValueError("kaboom")

    with app.test_request_context("/boom", headers={"User-Agent": "probe-agent"}):
        app.preprocess_request()
        with pytest.raises(ValueError, match="kaboom"):
            app.view_functions["boom"]()

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT status_code, error_message, client_ip, user_agent FROM metrics"
        ).fetchall()
    finally:
        conn.close()
    assert rows == [(500, "kaboom", None, None)]


def test_opportunistic_purge_on_store(tmp_path, sync_threads):
    db_path = str(tmp_path / "metrics.db")
    collector = MetricsCollector(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO metrics (timestamp, endpoint, method, status_code, response_time)"
        " VALUES (?, 'old', 'GET', 200, 0.1)",
        (int(time.time()) - 40 * 86400,),
    )
    conn.commit()
    conn.close()

    collector._last_purge = time.time() - 2 * 86400
    collector.record_request("new", "GET", 200, 0.01)

    assert [r[0] for r in _rows(db_path)] == ["new"]


def test_failed_purge_keeps_metric_row(tmp_path, sync_threads, monkeypatch):
    db_path = str(tmp_path / "metrics.db")
    collector = MetricsCollector(db_path)

    def _locked(conn):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(collector, "_purge_old_metrics", _locked)
    collector._last_purge = 0
    collector.record_request("kept", "GET", 200, 0.01)

    assert [r[0] for r in _rows(db_path)] == ["kept"]


def test_init_scrubs_previously_stored_ip_and_ua(tmp_path):
    db_path = str(tmp_path / "metrics.db")
    MetricsCollector(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO metrics (timestamp, endpoint, method, status_code,"
        " response_time, client_ip, user_agent)"
        " VALUES (?, 'legacy', 'GET', 200, 0.1, '203.0.113.7', 'old-agent')",
        (int(time.time()),),
    )
    conn.commit()
    conn.close()

    MetricsCollector(db_path)

    assert _rows(db_path)[0][4:] == (None, None)


def test_real_app_records_through_container(tmp_path, sync_threads, monkeypatch):
    """The real create_app wiring: a monitored blueprint route writes a row to
    the container's DATABASE_PATH, and the health flag then reports it."""
    from app import app as real_app

    container = real_app.service_container
    db_path = str(tmp_path / "metrics.db")
    monkeypatch.setattr(container.config, "DATABASE_PATH", db_path)
    monkeypatch.setattr(container, "_metrics_collector", None)

    resp = real_app.test_client().get("/documentation")
    assert resp.status_code == 200

    assert [r[:3] for r in _rows(db_path)] == [("main.documentation", "GET", 200)]
    assert container._metrics_collector is not None
    assert container._metrics_collector.db_path == db_path


def test_old_rows_purged_at_init(tmp_path):
    db_path = str(tmp_path / "metrics.db")
    MetricsCollector(db_path)
    now = int(time.time())
    conn = sqlite3.connect(db_path)
    conn.executemany(
        "INSERT INTO metrics (timestamp, endpoint, method, status_code, response_time)"
        " VALUES (?, ?, 'GET', 200, 0.1)",
        [(now - 40 * 86400, "old"), (now - 86400, "recent")],
    )
    conn.commit()
    conn.close()

    MetricsCollector(db_path)

    assert [r[0] for r in _rows(db_path)] == ["recent"]


def test_store_failure_warns_once_then_debug(
    tmp_path, sync_threads, monkeypatch, caplog
):
    monkeypatch.setattr(monitoring, "_store_failure_warned", False, raising=False)
    # A directory is not an openable database file.
    collector = MetricsCollector(str(tmp_path))

    caplog.set_level(logging.DEBUG, logger=monitoring.__name__)
    for _ in range(3):
        collector.record_request("probe", "GET", 200, 0.01)

    failures = [r for r in caplog.records if "Failed to store metric" in r.getMessage()]
    assert [r.levelno for r in failures] == [
        logging.WARNING,
        logging.DEBUG,
        logging.DEBUG,
    ]


def test_system_health_with_null_ip_and_ua(tmp_path, sync_threads):
    collector = MetricsCollector(str(tmp_path / "metrics.db"))
    collector.record_request("probe", "GET", 200, 0.05)
    collector.record_request("probe", "GET", 500, 0.07)

    health = collector.get_system_health()
    assert health["total_requests_last_hour"] == 2
    assert health["total_errors_last_hour"] == 1
    assert collector.get_endpoint_stats("probe")["total_requests"] == 2
