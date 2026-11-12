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


class _ObservedFuture(Future):
    def __init__(self):
        super().__init__()
        self.wait_entered = threading.Event()

    def exception(self, timeout=None):
        self.wait_entered.set()
        return super().exception(timeout=timeout)


class _ObservedPool(ThreadPoolExecutor):
    def __init__(self):
        super().__init__(max_workers=1)
        self.wait_entered = threading.Event()

    def shutdown(self, wait=True, *, cancel_futures=False):
        if wait:
            self.wait_entered.set()
        return super().shutdown(wait=wait, cancel_futures=cancel_futures)


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


def test_drain_app_workers_waits_for_each_worker_barrier_individually():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)

        def spawn_future(name: str):
            entered, release, completed, write = _gated_writer(root, name)
            future = _ObservedFuture()

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
            return future, release, completed

        def spawn_pool(name: str):
            entered, release, completed, write = _gated_writer(root, name)
            pool = _ObservedPool()
            pool.submit(write)
            assert entered.wait(5)
            return pool, release, completed

        setup_future, setup_release, setup_done = spawn_future("companion-setup.txt")
        discarded = []
        setup_dialog = SimpleNamespace(
            closed=False,
            future=setup_future,
            discard=lambda: discarded.append(True),
        )

        settings_pool, settings_pool_release, settings_pool_done = spawn_pool("settings-pool.txt")
        settings_future, settings_future_release, settings_future_done = spawn_future("settings-future.txt")
        settings_closed = []
        settings_dialog = SimpleNamespace(
            closed=False,
            executor=settings_pool,
            _future=settings_future,
            force_close=lambda: settings_closed.append("force"),
            _closed=lambda: settings_closed.append("closed"),
        )

        conn_future, conn_release, conn_done = spawn_future("browser-connection.txt")
        health_future, health_release, health_done = spawn_future("browser-health.txt")
        conn_closed = []
        connection = SimpleNamespace(
            future=conn_future,
            health_future=health_future,
            close=lambda: conn_closed.append(True),
        )

        watch_pool, watch_pool_release, watch_pool_done = spawn_pool("browser-watch-pool.txt")
        watch_future, watch_future_release, watch_future_done = spawn_future("browser-watch-future.txt")
        watch_closed = []
        watch = SimpleNamespace(
            executor=watch_pool,
            future=watch_future,
            close=lambda: watch_closed.append(True),
        )

        monitor_pool, monitor_release, monitor_done = spawn_pool("monitor.txt")
        activity_pool, activity_release, activity_done = spawn_pool("activity.txt")
        search_pool, search_release, search_done = spawn_pool("search.txt")

        pub_entered, pub_release, pub_done, pub_write = _gated_writer(root, "publication.txt")
        pub_future = library_publication._ENCODER.submit(pub_write)
        assert pub_entered.wait(5)

        recovery_pool, recovery_release, recovery_done = spawn_pool("recovery.txt")
        trash_pool, trash_release, trash_done = spawn_pool("trash.txt")
        service_pool, service_pool_release, service_pool_done = spawn_pool("service-worker.txt")

        service_closed = threading.Event()
        service_release = threading.Event()
        service_exited = threading.Event()

        def run_service():
            assert service_closed.wait(5)
            assert service_release.wait(5)
            (root / "service-thread.txt").write_text("ok", encoding="utf-8")
            service_exited.set()

        service_thread = threading.Thread(target=run_service, daemon=True)
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

        # Verify each sequential barrier blocks the drainer before advancing to the next stage.
        steps = [
            (setup_future.wait_entered, settings_pool.wait_entered, setup_release, setup_done),
            (settings_pool.wait_entered, settings_future.wait_entered, settings_pool_release, settings_pool_done),
            (settings_future.wait_entered, conn_future.wait_entered, settings_future_release, settings_future_done),
            (conn_future.wait_entered, health_future.wait_entered, conn_release, conn_done),
            (health_future.wait_entered, watch_pool.wait_entered, health_release, health_done),
            (watch_pool.wait_entered, watch_future.wait_entered, watch_pool_release, watch_pool_done),
            (watch_future.wait_entered, monitor_pool.wait_entered, watch_future_release, watch_future_done),
            (monitor_pool.wait_entered, activity_pool.wait_entered, monitor_release, monitor_done),
            (activity_pool.wait_entered, search_pool.wait_entered, activity_release, activity_done),
        ]
        try:
            for current_entered, next_entered, release, completed in steps:
                assert current_entered.wait(5)
                assert not next_entered.is_set()
                assert not drain_done.is_set()
                release.set()
                assert completed.wait(5)

            assert search_pool.wait_entered.wait(5)
            assert not recovery_pool.wait_entered.is_set()
            search_release.set()
            assert search_done.wait(5)

            # After search_pool finishes, drainer must block on library_publication._ENCODER before recovery_pool.
            assert not recovery_pool.wait_entered.is_set()
            assert not drain_done.is_set()
            pub_release.set()
            assert pub_done.wait(5)

            assert recovery_pool.wait_entered.wait(5)
            assert not trash_pool.wait_entered.is_set()
            recovery_release.set()
            assert recovery_done.wait(5)

            assert trash_pool.wait_entered.wait(5)
            assert not service_closed.is_set()
            trash_release.set()
            assert trash_done.wait(5)

            assert service_closed.wait(5)
            assert not service_pool.wait_entered.is_set()
            service_release.set()
            assert service_exited.wait(5)

            assert service_pool.wait_entered.wait(5)
            assert not drain_done.is_set()
            service_pool_release.set()
            assert service_pool_done.wait(5)

            drainer.join(5)
            assert drain_done.is_set()
            assert not drain_errors, drain_errors
            assert discarded == [True]
            assert settings_closed == ["force", "closed"]
            assert conn_closed == [True]
            assert watch_closed == [True]
            assert pub_future.done()
        finally:
            for gate in (
                setup_release,
                settings_pool_release,
                settings_future_release,
                conn_release,
                health_release,
                watch_pool_release,
                watch_future_release,
                monitor_release,
                activity_release,
                search_release,
                pub_release,
                recovery_release,
                trash_release,
                service_release,
                service_pool_release,
            ):
                gate.set()
