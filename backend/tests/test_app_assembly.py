"""W15: the app factory, the ordered lifespan, the dependencies and the bootstrap route.

Everything here runs a real application against a temporary data directory. Nothing is mocked out
of the startup path: SQLite is opened and migrated, the key hierarchy is provisioned onto a real
key file, DuckDB is opened and its writer task is started, and the whole thing is shut down again.
A lifespan that is only reasoned about is a lifespan that leaks a task.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from expirymanager import lifespan as lifespan_module
from expirymanager.api import errors as errors_module
from expirymanager.api import static as static_module
from expirymanager.api import v1 as api_v1
from expirymanager.api.schemas.common import (
    CursorError,
    Page,
    RequestModel,
    decode_cursor,
    encode_cursor,
)
from expirymanager.app import ALLOWED_HOSTS, create_app
from expirymanager.db import sqlite as sqlite_module
from expirymanager.db.writer import CallableWrite, WriterError
from expirymanager.security.headers import CONTENT_SECURITY_POLICY, NO_STORE
from expirymanager.version import __version__

BASE_URL = "https://127.0.0.1:8000"

SYNTHETIC_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJzdWIiOiJzeW50aGV0aWMiLCJleHAiOjk5OTk5OTk5OTl9."
    "c3ludGhldGljLXNpZ25hdHVyZS1ub3QtcmVhbA"
)


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """A private data directory, and a guarantee that nothing reaches the real one."""
    root = tmp_path / "expirymanager-home"
    monkeypatch.setattr("expirymanager.paths.default_root", lambda: root)
    yield root
    sqlite_module.dispose_engine()


@pytest.fixture
def app(data_dir):
    application = create_app(root=data_dir, serve_static=False)
    yield application
    sqlite_module.dispose_engine()


@pytest.fixture
def client(app):
    with TestClient(app, base_url=BASE_URL) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# Cold start
# ---------------------------------------------------------------------------


class TestColdStart:
    def test_it_boots_from_an_empty_home_and_creates_every_store(self, data_dir):
        assert not data_dir.exists()

        application = create_app(root=data_dir, serve_static=False)
        with TestClient(application, base_url=BASE_URL) as client:
            assert client.get("/api/v1/bootstrap").status_code == 200

        paths = application.state.services.paths
        assert paths.root.exists()
        assert paths.sqlite_db.exists()
        assert paths.duckdb_file.exists()
        assert paths.master_key.exists()
        sqlite_module.dispose_engine()

    def test_the_master_key_and_the_database_are_owner_only(self, client, app):
        paths = app.state.services.paths
        assert paths.master_key.stat().st_mode & 0o077 == 0
        assert paths.sqlite_db.stat().st_mode & 0o077 == 0

    def test_startup_runs_the_documented_sequence_in_order(self):
        names = [step.name for step in lifespan_module.STARTUP_SEQUENCE]
        assert names == [
            "sqlite",
            "migrations",
            "settings",
            "keys",
            "duckdb",
            "http_security",
            "fyers",
            lifespan_module.SLOT_PIPELINE_SUPERVISOR,
            lifespan_module.SLOT_JOB_RECOVERY,
            lifespan_module.SLOT_SCHEDULER,
        ]

    def test_every_service_is_built_and_the_schema_is_migrated(self, client, app):
        state = app.state.services
        assert state.ready is True
        assert state.engine is not None
        assert state.settings is not None
        assert state.key_manager is not None
        assert state.duck is not None
        assert state.session_manager is not None
        assert state.rate_limiter is not None
        assert state.governor is not None
        assert state.token_broker is not None
        assert state.fyers_client is not None

        with state.engine.connect() as connection:
            version = connection.execute(text("SELECT max(version) FROM schema_version")).scalar()
        assert version >= 4

    def test_the_settings_store_is_the_seeded_one_and_there_is_only_one(self, client, app):
        settings = app.state.services.settings
        assert settings.get_int("throttle_per_second") == 8
        assert settings.get_int("throttle_per_minute") == 170
        assert app.state.services.governor.snapshot().per_minute == 170

    def test_starting_twice_over_the_same_directory_is_idempotent(self, data_dir):
        for _ in range(2):
            application = create_app(root=data_dir, serve_static=False)
            with TestClient(application, base_url=BASE_URL) as client:
                assert client.get("/api/v1/bootstrap").json()["provisioned"] is True
            sqlite_module.dispose_engine()

    def test_the_lifespan_does_not_take_a_second_instance_lock(self, data_dir, client, app):
        """`__main__` already holds it, and a second acquire from one process raises."""
        from expirymanager import paths as paths_module

        lock = paths_module.InstanceLock(app.state.services.paths.lock_file)
        # If the lifespan had taken the lock, this would raise SingleInstanceError.
        lock.acquire()
        lock.release()


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


class TestShutdown:
    def test_the_writer_task_is_not_left_running(self, data_dir):
        application = create_app(root=data_dir, serve_static=False)
        with TestClient(application, base_url=BASE_URL) as client:
            client.get("/api/v1/bootstrap")
            writer = application.state.services.duck.writer
            assert writer.running is True

        assert writer.running is False
        assert application.state.services.duck is None
        # The task is gone, not merely flagged as stopped: the writer refuses work rather than
        # queueing onto a consumer that will never run again.
        with pytest.raises(WriterError):
            asyncio.run(writer.submit(_never_applied()))
        sqlite_module.dispose_engine()

    def test_shutdown_closes_duckdb_and_disposes_the_engine(self, data_dir):
        application = create_app(root=data_dir, serve_static=False)
        with TestClient(application, base_url=BASE_URL) as client:
            client.get("/api/v1/bootstrap")
            store = application.state.services.duck
            assert store.is_open is True

        assert store.is_open is False
        assert application.state.services.engine is None
        assert application.state.services.ready is False
        sqlite_module.dispose_engine()

    async def test_a_failing_step_tears_down_only_what_started(self, data_dir):
        from expirymanager import paths as paths_module

        state = lifespan_module.AppState(paths=paths_module.ensure(data_dir, ensure_tls=False))
        stopped: list[str] = []

        def boom(_state):
            raise RuntimeError("synthetic step failure")

        sequence = (
            lifespan_module.LifecycleStep(
                "first", lambda s: None, lambda s: stopped.append("first")
            ),
            lifespan_module.LifecycleStep(
                "second", lambda s: None, lambda s: stopped.append("second")
            ),
            lifespan_module.LifecycleStep("third", boom, lambda s: stopped.append("third")),
            lifespan_module.LifecycleStep(
                "fourth", lambda s: None, lambda s: stopped.append("fourth")
            ),
        )

        with pytest.raises(RuntimeError):
            await lifespan_module.run_startup(state, sequence)

        # Reverse order, and neither the step that raised nor the one after it.
        assert stopped == ["second", "first"]
        assert state.ready is False

    async def test_one_failing_teardown_does_not_skip_the_others(self, data_dir):
        from expirymanager import paths as paths_module

        state = lifespan_module.AppState(paths=paths_module.ensure(data_dir, ensure_tls=False))
        stopped: list[str] = []

        def bad_stop(_state):
            raise RuntimeError("synthetic teardown failure")

        started = [
            lifespan_module.LifecycleStep("a", lambda s: None, lambda s: stopped.append("a")),
            lifespan_module.LifecycleStep("b", lambda s: None, bad_stop),
            lifespan_module.LifecycleStep("c", lambda s: None, lambda s: stopped.append("c")),
        ]
        await lifespan_module.run_shutdown(state, started)
        assert stopped == ["c", "a"]


# ---------------------------------------------------------------------------
# The registration points for W11 and W14
# ---------------------------------------------------------------------------


@pytest.fixture
def bare_registry():
    """Empty the component registry for one test, then put it back.

    create_app now installs the pipeline, the job recovery step and the scheduler, which is what
    makes the application actually work. These tests are about the registration MECHANISM rather
    than about what production happens to register, so they need a registry they control. Without
    this they assert against whatever the last create_app left behind, which is both order
    dependent and a test of the wrong thing.
    """
    saved = dict(lifespan_module._component_factories)
    lifespan_module._component_factories.clear()
    try:
        yield
    finally:
        lifespan_module._component_factories.clear()
        lifespan_module._component_factories.update(saved)


class TestRegistrationPoints:
    def test_the_app_serves_with_every_slot_empty(self, client, bare_registry):
        # A slot with no factory must not stop the application starting. That is what let the
        # phases ship in dependency order while later components did not exist yet.
        assert lifespan_module.registered_components() == ()
        assert client.get("/api/v1/bootstrap").status_code == 200

    def test_a_registered_component_is_started_and_stopped_in_place(self, data_dir, bare_registry):
        events: list[str] = []

        class FakeSupervisor:
            def __init__(self, state):
                self.state = state

            async def start(self):
                events.append("supervisor_start")

            async def stop(self):
                events.append("supervisor_stop")

        class FakeScheduler:
            async def start(self):
                events.append("scheduler_start")

            async def shutdown(self):
                events.append("scheduler_shutdown")

        def recovery(state):
            # A slot may only have work to do and nothing to hold.
            events.append("recovery")
            return None

        lifespan_module.register_component(
            lifespan_module.SLOT_PIPELINE_SUPERVISOR, FakeSupervisor
        )
        lifespan_module.register_component(lifespan_module.SLOT_JOB_RECOVERY, recovery)
        lifespan_module.register_component(
            lifespan_module.SLOT_SCHEDULER, lambda state: FakeScheduler()
        )
        try:
            application = create_app(root=data_dir, serve_static=False)
            with TestClient(application, base_url=BASE_URL) as client:
                assert client.get("/api/v1/bootstrap").status_code == 200
                assert application.state.services.supervisor is not None
                assert application.state.services.scheduler is not None
        finally:
            for slot in lifespan_module.COMPONENT_SLOTS:
                lifespan_module.unregister_component(slot)
            sqlite_module.dispose_engine()

        assert events == [
            "supervisor_start",
            "recovery",
            "scheduler_start",
            "scheduler_shutdown",
            "supervisor_stop",
        ]

    def test_an_unknown_slot_is_refused(self):
        with pytest.raises(ValueError):
            lifespan_module.register_component("not_a_slot", lambda state: None)

    def test_a_route_needing_the_pipeline_gets_the_documented_503_when_the_slot_is_empty(
        self, client, app
    ):
        from expirymanager.api.deps import get_supervisor
        from expirymanager.api.errors import ApiError

        # Emptying the factory registry is not enough here: the lifespan has already run, so the
        # supervisor is a live entry in state.components. What this test is about is a route
        # reached while the pipeline is genuinely absent, so remove the started component itself
        # and put it back afterwards.
        components = app.state.services.components
        removed = components.pop(lifespan_module.SLOT_PIPELINE_SUPERVISOR, None)
        try:
            with pytest.raises(ApiError) as caught:
                get_supervisor(app.state.services)
            assert caught.value.status_code == 503
            assert caught.value.code == "pipeline_stopped"
        finally:
            if removed is not None:
                components[lifespan_module.SLOT_PIPELINE_SUPERVISOR] = removed

    def test_the_pipeline_dependency_resolves_once_the_component_is_present(self, client, app):
        # The other half of the same contract, and the half that regressed: for a long time every
        # slot was empty in production, so this dependency always raised and nothing noticed.
        from expirymanager.api.deps import get_supervisor

        assert get_supervisor(app.state.services) is not None


# ---------------------------------------------------------------------------
# The bootstrap route
# ---------------------------------------------------------------------------


class TestBootstrapRoute:
    def test_a_fresh_install_reports_needs_setup(self, client):
        body = client.get("/api/v1/bootstrap").json()
        assert body["provisioned"] is True
        assert body["has_user"] is False
        assert body["has_credentials"] is False
        assert body["broker_connected"] is False
        assert body["token_state"] == "none"
        assert body["needs_reauth"] is False
        assert body["app_version"] == __version__
        assert body["duckdb_version"]

    def test_it_is_reachable_with_no_session(self, client):
        # No cookie is sent at all, and the route still answers.
        assert "em_session" not in client.cookies
        assert client.get("/api/v1/bootstrap").status_code == 200

    def test_a_user_row_flips_it_to_needs_login(self, client, app):
        _insert_user(app.state.services.engine)
        body = client.get("/api/v1/bootstrap").json()
        assert body["provisioned"] is True
        assert body["has_user"] is True
        assert body["has_credentials"] is False

    def test_credentials_with_no_token_report_needs_reauth(self, client, app):
        state = app.state.services
        _insert_user(state.engine)
        _insert_credential(state.engine, state.key_manager)
        state.token_broker.reload()

        body = client.get("/api/v1/bootstrap").json()
        assert body["has_credentials"] is True
        assert body["broker_connected"] is False
        assert body["needs_reauth"] is True

    async def test_a_stored_token_reports_connected(self, client, app):
        state = app.state.services
        _insert_user(state.engine)
        _insert_credential(state.engine, state.key_manager)
        state.token_broker.reload()
        await state.token_broker.store_login(access_token=SYNTHETIC_JWT, refresh_token=None)

        body = client.get("/api/v1/bootstrap").json()
        assert body["broker_connected"] is True
        assert body["needs_reauth"] is False
        assert body["token_state"] == "active"

    def test_the_response_never_carries_the_data_of_a_secret(self, client, app):
        state = app.state.services
        _insert_credential(state.engine, state.key_manager)
        raw = client.get("/api/v1/bootstrap").text
        assert FAKE_APP_SECRET not in raw
        assert "app_secret" not in raw


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class TestMiddleware:
    def test_the_order_is_the_documented_one(self, app):
        names = [middleware.cls.__name__ for middleware in app.user_middleware]
        assert names == [
            "CorrelationIdMiddleware",
            "TrustedHostMiddleware",
            "SecurityHeadersMiddleware",
            "SessionMiddleware",
            "CsrfMiddleware",
            "RateLimitMiddleware",
        ]

    def test_the_security_headers_are_present_on_an_api_response(self, client):
        response = client.get("/api/v1/bootstrap")
        assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["cross-origin-opener-policy"] == "same-origin"
        assert "permissions-policy" in response.headers

    def test_every_api_response_is_no_store(self, client):
        assert client.get("/api/v1/bootstrap").headers["cache-control"] == NO_STORE

    def test_hsts_is_never_sent_on_loopback(self, client):
        assert "strict-transport-security" not in client.get("/api/v1/bootstrap").headers

    def test_a_foreign_host_is_rejected(self, client):
        response = client.get("/api/v1/bootstrap", headers={"host": "evil.example.com"})
        assert response.status_code == 400

    def test_the_allowlist_is_the_documented_one(self):
        assert ALLOWED_HOSTS == ["127.0.0.1", "127.0.0.1:8000", "127.0.0.1:5173"]

    def test_the_rate_limit_headers_are_attached(self, client):
        response = client.get("/api/v1/bootstrap")
        assert response.headers["ratelimit-limit"]
        assert int(response.headers["ratelimit-remaining"]) >= 0

    def test_the_bootstrap_limit_is_enforced_at_sixty_per_minute(self, client):
        # The documented rule is 60/minute per ip. The 61st is refused.
        statuses = [client.get("/api/v1/bootstrap").status_code for _ in range(61)]
        assert statuses.count(200) == 60
        assert statuses[-1] == 429

    def test_csrf_exempts_exactly_the_callback(self, app):
        for middleware in app.user_middleware:
            if middleware.cls.__name__ == "CsrfMiddleware":
                assert middleware.kwargs["exempt_paths"] == ("/fyers/callback",)
                break
        else:  # pragma: no cover - the previous test would have caught this
            pytest.fail("the csrf middleware is not installed")

    def test_the_session_is_resolved_into_request_state(self, client, app):
        # No session cookie, so the middleware resolves None rather than rejecting.
        response = client.get("/api/v1/bootstrap")
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Correlation id and the error envelope
# ---------------------------------------------------------------------------


def _error_app(**kwargs):
    """A one-route application used to exercise the handlers."""
    from expirymanager.api.errors import ApiError

    router = APIRouter()

    @router.get("/boom/api-error")
    async def _api_error():
        raise ApiError(409, "needs_reauth", "The Fyers session has ended.")

    @router.get("/boom/unhandled")
    async def _unhandled():
        raise RuntimeError(f"upstream said access_token={SYNTHETIC_JWT}")

    @router.get("/boom/http")
    async def _http():
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="No such contract.")

    @router.get("/boom/validation")
    async def _validation(contract_id: int, note: str = ""):
        return {"contract_id": contract_id, "note": note}

    @router.get("/boom/cursor")
    async def _cursor(cursor: str):
        # A paging route decodes without wrapping. The handler is what makes that safe.
        return decode_cursor(cursor, expect=("job_id",))

    return router


class _LoginLike(RequestModel):
    username: str
    password: str


@pytest.fixture
def error_client(data_dir):
    application = create_app(root=data_dir, serve_static=False)
    application.include_router(_error_app(), prefix="/api/v1")
    with TestClient(
        application, base_url=BASE_URL, raise_server_exceptions=False
    ) as test_client:
        yield test_client
    sqlite_module.dispose_engine()


class TestErrors:
    def test_the_correlation_id_is_on_every_response_and_changes(self, client):
        first = client.get("/api/v1/bootstrap").headers["x-correlation-id"]
        second = client.get("/api/v1/bootstrap").headers["x-correlation-id"]
        assert first and second and first != second

    def test_an_api_error_renders_the_documented_envelope(self, error_client):
        response = error_client.get("/api/v1/boom/api-error")
        assert response.status_code == 409
        body = response.json()["error"]
        assert body["code"] == "needs_reauth"
        assert body["message"] == "The Fyers session has ended."
        assert body["correlation_id"] == response.headers["x-correlation-id"]

    def test_an_unhandled_exception_leaks_neither_the_token_nor_a_traceback(
        self, error_client, caplog
    ):
        with caplog.at_level(logging.ERROR):
            response = error_client.get("/api/v1/boom/unhandled")
        assert response.status_code == 500
        raw = response.text
        assert SYNTHETIC_JWT not in raw
        assert "Traceback" not in raw
        assert "RuntimeError" not in raw
        body = response.json()["error"]
        assert body["code"] == "internal_error"
        assert body["correlation_id"]

        # The detail reached the log, and reached it redacted.
        logged = " ".join(
            str(getattr(record, "error_detail", "")) for record in caplog.records
        )
        assert SYNTHETIC_JWT not in logged
        assert logged.strip() != ""

    def test_a_five_hundred_still_carries_the_security_headers(self, error_client):
        response = error_client.get("/api/v1/boom/unhandled")
        assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
        assert response.headers["cache-control"] == NO_STORE

    def test_an_http_exception_maps_to_a_named_code(self, error_client):
        response = error_client.get("/api/v1/boom/http")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"
        assert response.json()["error"]["message"] == "No such contract."

    def test_a_validation_error_renders_the_envelope_and_echoes_no_value(self, error_client):
        response = error_client.get(
            "/api/v1/boom/validation",
            params={"contract_id": "synthetic-password-value"},
        )
        assert response.status_code == 422
        body = response.json()["error"]
        assert body["code"] == "validation_error"
        assert body["correlation_id"]
        assert body["detail"]["errors"][0]["loc"] == ["query", "contract_id"]
        # The submitted value is what a naive Pydantic error list would carry straight back.
        assert "synthetic-password-value" not in response.text

    def test_pydantic_input_and_ctx_are_stripped(self):
        errors = [
            {
                "loc": ("body", "password"),
                "msg": "Field required",
                "type": "missing",
                "input": {"password": "synthetic-password-value"},
                "ctx": {"error": "synthetic-password-value"},
            }
        ]
        safe = errors_module._safe_validation_errors(errors)
        assert safe == [{"loc": ["body", "password"], "type": "missing", "msg": "Field required"}]
        assert "synthetic-password-value" not in repr(safe)

    def test_a_tampered_cursor_is_a_four_hundred_and_not_a_five_hundred(self, error_client):
        response = error_client.get("/api/v1/boom/cursor", params={"cursor": "tampered!!"})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_cursor"

    def test_a_not_found_api_route_gets_the_envelope(self, client):
        response = client.get("/api/v1/nothing-here")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# The router include list
# ---------------------------------------------------------------------------


class TestRouterList:
    def test_every_route_module_from_w16_to_w20_is_listed(self):
        listed = {spec.module for spec in api_v1.ROUTER_SPECS}
        assert listed == {
            "bootstrap",
            "auth",
            "broker",
            "underlyings",
            "expiries",
            "contracts",
            "downloads",
            "jobs",
            "coverage",
            "bars",
            "chain",
            "exports",
            "schedules",
            "system",
            "events",
        }

    def test_pending_names_exactly_the_route_modules_with_no_file_yet(self):
        """The list shrinks as W16 to W20 land, so the assertion is against the files on disk.

        It read `assert "auth" in pending` while W16 was unbuilt, which stopped being true the
        moment W16 created `api/v1/auth.py`. Stated against the package directory it keeps saying
        the same thing, that `missing_modules` reports every spec with no module and nothing else,
        for the whole of the build rather than for one afternoon of it.
        """
        package_dir = Path(api_v1.__file__).parent
        expected = {
            spec.module
            for spec in api_v1.ROUTER_SPECS
            if not (package_dir / f"{spec.module}.py").exists()
        }

        assert set(api_v1.missing_modules()) == expected
        assert "bootstrap" not in expected

    def test_a_broken_import_inside_a_route_module_is_not_swallowed(self, monkeypatch):
        def explode(name, *args, **kwargs):
            raise ModuleNotFoundError("No module named 'not_installed'", name="not_installed")

        monkeypatch.setattr(api_v1.importlib, "import_module", explode)
        with pytest.raises(ModuleNotFoundError):
            api_v1.build_router()

    def test_the_oauth_callback_is_included_at_the_root_not_under_api(self, app):
        # W16 has not created the module yet, so no route exists. What is asserted here is that
        # the include is at the root: nothing under /api/v1 claims that path.
        from expirymanager.app import OAUTH_CALLBACK_MODULE

        assert OAUTH_CALLBACK_MODULE == "expirymanager.api.oauth_callback"
        paths = {route.path for route in app.routes if hasattr(route, "path")}
        assert "/api/v1/fyers/callback" not in paths


# ---------------------------------------------------------------------------
# Static
# ---------------------------------------------------------------------------


class TestStatic:
    def test_nothing_is_mounted_without_a_build(self, app):
        assert not [route for route in app.routes if getattr(route, "name", "") == "spa"]

    def test_the_spa_fallback_serves_index_for_a_client_route(self, data_dir, tmp_path):
        dist = tmp_path / "dist"
        (dist / "assets").mkdir(parents=True)
        (dist / "index.html").write_text("<title>ExpiryManager</title>", encoding="utf-8")
        (dist / "assets" / "app.js").write_text("export default 1", encoding="utf-8")

        application = create_app(root=data_dir, dist_dir=dist, serve_static=True)
        with TestClient(application, base_url=BASE_URL) as client:
            assert client.get("/assets/app.js").status_code == 200
            deep = client.get("/jobs/1841")
            assert deep.status_code == 200
            assert "ExpiryManager" in deep.text
            # The security headers reach the SPA as well as the API.
            assert deep.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
            # An unknown API path is still a JSON 404, not the index page.
            missing = client.get("/api/v1/does-not-exist")
            assert missing.status_code == 404
            assert missing.json()["error"]["code"] == "not_found"
        sqlite_module.dispose_engine()

    def test_a_directory_without_an_index_is_not_mounted(self, tmp_path):
        empty = tmp_path / "empty-dist"
        empty.mkdir()
        application = FastAPI()
        assert static_module.mount_spa(application, empty) is None


# ---------------------------------------------------------------------------
# Shared schemas
# ---------------------------------------------------------------------------


class TestCommonSchemas:
    def test_a_cursor_round_trips(self):
        cursor = encode_cursor({"job_id": 41})
        assert decode_cursor(cursor, expect=("job_id",)) == {"job_id": 41}
        assert "=" not in cursor

    def test_a_cursor_from_another_collection_is_refused(self):
        cursor = encode_cursor({"job_id": 41})
        with pytest.raises(CursorError):
            decode_cursor(cursor, expect=("contract_id",))

    def test_rubbish_is_refused_rather_than_reaching_a_where_clause(self):
        for value in ("not-base64!!", "", encode_cursor({})[:2] + "@@"):
            with pytest.raises(CursorError):
                decode_cursor(value, expect=("job_id",))

    def test_a_request_model_forbids_unknown_fields(self):
        with pytest.raises(Exception):
            _LoginLike(username="a", password="b", extra="c")

    def test_a_page_defaults_to_empty_with_no_cursor(self):
        page: Page[int] = Page()
        assert page.items == []
        assert page.next_cursor is None


# ---------------------------------------------------------------------------
# Helpers. Every value is synthetic.
# ---------------------------------------------------------------------------

def _never_applied() -> CallableWrite:
    """A write op the stopped writer must refuse before it can ever run."""
    return CallableWrite(label="synthetic", fn=lambda cur: None)


FAKE_APP_ID = "SYNTHETIC00-100"
FAKE_APP_SECRET = "synthetic-app-secret-value"
CREDENTIAL_ID = "00000000-0000-4000-8000-000000000001"


def _insert_user(engine) -> str:
    from datetime import UTC, datetime

    user_id = "00000000-0000-4000-8000-0000000000aa"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO app_user (user_id, username, password_phc, created_at) "
                "VALUES (:uid, 'synthetic', 'synthetic-phc-not-a-real-hash', :now)"
            ),
            {"uid": user_id, "now": datetime.now(UTC).isoformat()},
        )
    return user_id


def _insert_credential(engine, key_manager) -> None:
    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat()
    secret_enc = key_manager.encrypt_field(
        FAKE_APP_SECRET,
        table="broker_credential",
        column="app_secret_enc",
        row_id=CREDENTIAL_ID,
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO broker_credential (credential_id, broker, label, app_id,"
                " app_secret_enc, redirect_uri, plan, key_ver, is_active, created_at, updated_at)"
                " VALUES (:cid, 'fyers', 'Synthetic', :app_id, :secret,"
                " 'https://127.0.0.1:8000/fyers/callback', 'standard', :key_ver, 1, :now, :now)"
            ),
            {
                "cid": CREDENTIAL_ID,
                "app_id": FAKE_APP_ID,
                "secret": secret_enc,
                "key_ver": key_manager.active_version,
                "now": now,
            },
        )
