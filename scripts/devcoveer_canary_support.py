"""Bounded local canary execution; never prints exception bodies or credentials."""
from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import os
from pathlib import Path
import re
import time


def load_candidate_installer():
    path = Path(__file__).resolve().parents[1] / "deploy" / "devcoveer_install.py"
    spec = importlib.util.spec_from_file_location("canary_candidate_installer", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def use_installed_provider_environment():
    """Reuse only the installed provider binding and this candidate's allowlist.

    No path/value arguments, no HOST_ENV/service.env reads, and no output of
    keys or values. This mutates only the isolated canary process environment.
    """
    installer = load_candidate_installer()
    values = installer.select_provider_environment(installer._parse_env(installer.PROVIDER_ENV))
    os.environ.update(values)


def record_error(report, exc, *, phase="run"):
    report["passed"] = False
    item = {"phase": phase, "error_type": type(exc).__name__}
    code = str(exc)
    if re.fullmatch(r"CANARY_[A-Z0-9_]{1,100}", code):
        item["error_code"] = code
    report.setdefault("errors", []).append(item)


def save_report(path: Path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".writing")
    with temporary.open("w", encoding="utf-8") as output:
        os.chmod(temporary, 0o600)
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


def _consume(task):
    if not task.cancelled():
        task.exception()


async def bounded(awaitable, timeout):
    """Unlike wait_for, a late cancellation acknowledgement cannot extend this wait.

    A timed-out mutating operation has an unknown outcome. Callers must fail the
    gate and run ordinary cleanup; this helper never retries it or refunds a grant.
    """
    task = asyncio.ensure_future(awaitable)
    try:
        done, _ = await asyncio.wait({task}, timeout=max(0, timeout))
        if task not in done:
            raise TimeoutError("CANARY_ASYNC_DEADLINE")
        return task.result()
    finally:
        if not task.done():
            task.cancel()
            task.add_done_callback(_consume)


async def cleanup_step(awaitable, report, phase, deadline):
    try:
        await bounded(awaitable, deadline - time.monotonic())
        return True
    except BaseException as exc:
        record_error(report, exc, phase=phase)
        return False


async def _finish_loop(report):
    tasks = {task for task in asyncio.all_tasks() if task is not asyncio.current_task() and not task.done()}
    for task in tasks:
        task.cancel()
    if tasks:
        done, pending = await asyncio.wait(tasks, timeout=5)
        for task in done:
            _consume(task)
        report["loop_tasks_unjoined"] = len(pending)
        if pending:
            record_error(report, TimeoutError("CANARY_LOOP_TASKS_UNJOINED"), phase="loop_shutdown")
    loop = asyncio.get_running_loop()
    await cleanup_step(loop.shutdown_asyncgens(), report, "asyncgens_shutdown", time.monotonic() + 2)
    # Python >=3.12/uvloop expose a real executor shutdown bound. Older loops
    # are closed below with wait=False; all canary HTTP requests have timeouts.
    if "timeout" in inspect.signature(loop.shutdown_default_executor).parameters:
        await cleanup_step(loop.shutdown_default_executor(timeout=2), report,
                           "executor_shutdown", time.monotonic() + 3)


def run_reported(report_path, report, operation):
    """Persist failures even before setup and before any potentially failed teardown.

    Product/provider coroutines still receive cancellation and ordinary cleanup.
    A task refusing to join is a failed gate, never a successful lease release.
    Operators should retain their existing process timeout for a blocked OS call.
    """
    save_report(report_path, report)
    try:
        import uvloop
        loop = uvloop.new_event_loop()
    except ImportError:
        loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(operation(report))
    except BaseException as exc:
        record_error(report, exc)
    finally:
        checks_passed = bool(report.get("passed"))
        report["checks_passed_before_loop_shutdown"] = checks_passed
        report["passed"] = False
        save_report(report_path, report)
        try:
            loop.run_until_complete(_finish_loop(report))
        except BaseException as exc:
            record_error(report, exc, phase="loop_shutdown")
        finally:
            try:
                loop.close()
            except BaseException as exc:
                record_error(report, exc, phase="loop_close")
            asyncio.set_event_loop(None)
            report["passed"] = checks_passed and not report.get("errors")
            save_report(report_path, report)
    return report
