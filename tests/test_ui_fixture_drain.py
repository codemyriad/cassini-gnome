# Copyright 2026 Codemyriad OÜ
# SPDX-License-Identifier: GPL-3.0-or-later
"""Event-gated regression tests for UI fixture worker teardown before directory removal."""

import importlib.util
import tempfile
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from cassini_talk import library_publication

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("dispatch_wait", ROOT / "tests/ui/dispatch_wait.py")
dispatch_wait = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dispatch_wait)


def _gated_writer(directory: Path, name: str):
    entered = threading.Event()
    release = threading.Event()
    completed = threading.Event()

    def write():
        entered.set()
        assert release.wait(5), f"Gate never released for {name}"
        target = directory / name
        target.write_text("ok", encoding="utf-8")
        completed.set()

    return entered, release, completed, write


def test_drain_app_workers_waits_for_all_inflight_fixture_writers_before_tempdir_cleanup():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        gates = []

        def spawn_future(name: str) -> Future:
            entered, release, completed, write = _gated_writer(root, name)
            gates.append((entered, release, completed))
            future = Future()

            def run():
                assert future.set_running_or_notify_cancel()
                try:
                    write()
                except Exception as error:
                    future.set_exception(error)
                else:
                    future.set_result(None)

            threading.Thread(target=run, daemon=True).start()
            assert entered.wait(5)
            return future

        def spawn_pool(name: str):
            entered, release, completed, write = _gated_writer(root, name)
            gates.append((entered, release, completed))
            pool = ThreadPoolExecutor(max_workers=1)
            future = pool.submit(write)
            assert entered.wait(5)
            return pool, future

        setup_future = spawn_future("companion-setup.txt")
        discarded = []
        setup_dialog = SimpleNamespace(
            closed=False,
            future=setup_future,
            discard=lambda: discarded.append(True),
        )

        settings_pool, settings_future = spawn_pool("settings.txt")
        settings_closed = []
        settings_dialog = SimpleNamespace(
            closed=False,
            executor=settings_pool,
            _future=settings_future,
            force_close=lambda: settings_closed.append("force"),
            _closed=lambda: settings_closed.append("closed"),
        )

        conn_future = spawn_future("browser-connection.txt")
        health_future = spawn_future("browser-health.txt")
        conn_closed = []
        connection = SimpleNamespace(
            future=conn_future,
            health_future=health_future,
            close=lambda: conn_closed.append(True),
        )

        watch_pool, watch_future = spawn_pool("browser-watch.txt")
        watch_closed = []
        watch = SimpleNamespace(
            executor=watch_pool,
            future=watch_future,
            close=lambda: watch_closed.append(True),
        )

        pub_entered, pub_release, pub_completed, pub_write = _gated_writer(root, "publication.txt")
        gates.append((pub_entered, pub_release, pub_completed))
        pub_future = library_publication._ENCODER.submit(pub_write)
        assert pub_entered.wait(5)

        recovery_pool, _ = spawn_pool("recovery.txt")
        trash_pool, _ = spawn_pool("trash.txt")
        monitor_pool, _ = spawn_pool("monitor.txt")
        activity_pool, _ = spawn_pool("activity.txt")
        search_pool, _ = spawn_pool("search.txt")
        service_pool, _ = spawn_pool("service-worker.txt")

        service_closed = threading.Event()
        service_exited = threading.Event()
        service_thread = threading.Thread(
            target=lambda: (assert_wait := service_closed.wait(5), service_exited.set(), assert_wait),
            daemon=True,
        )
        service_thread.start()
        service = SimpleNamespace(closed=service_closed, workers=service_pool)

        window = SimpleNamespace(
            monitor=SimpleNamespace(_executor=monitor_pool),
            _activity_source=None,
            search_page=None,
            _import_executor=None,
        )
        app = SimpleNamespace(
            companion_setup=None,
            settings_dialog=None,
            browser_connection=connection,
            browser_watch=watch,
            native_captures={},
            library_recovery=SimpleNamespace(executor=recovery_pool),
            meeting_trash=SimpleNamespace(executor=trash_pool),
            voice_backfill=None,
            library_relocator=None,
            comfort=None,
            transcriber=None,
            _record_start_executor=None,
        )

        drain_done = threading.Event()
        drain_errors = []

        def run_drain():
            try:
                dispatch_wait.drain_app_workers(
                    app,
                    windows=(window,),
                    setup_dialogs=(setup_dialog,),
                    settings_dialogs=(settings_dialog,),
                    activity_sources=(SimpleNamespace(executor=activity_pool),),
                    search_pages=(SimpleNamespace(_executor=search_pool),),
                    service=service,
                    service_thread=service_thread,
                )
            except BaseException as error:
                drain_errors.append(error)
            finally:
                drain_done.set()

        drainer = threading.Thread(target=run_drain, daemon=True)
        drainer.start()

        # Drain must block while writers are still in flight, then finish cleanly once released.
        assert not drain_done.is_set()
        for _, release, _ in gates:
            release.set()

        drainer.join(5)
        assert drain_done.is_set()
        assert not drain_errors, drain_errors
        assert discarded == [True]
        assert settings_closed == ["force", "closed"]
        assert conn_closed == [True]
        assert watch_closed == [True]
        assert pub_future.done()
        assert service_exited.is_set()
        assert all(completed.is_set() for _, _, completed in gates)
