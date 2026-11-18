"""Per-operation timings scoped to the active job, without request bodies or secrets."""

import time
from contextlib import contextmanager
from contextvars import ContextVar

_active = ContextVar("generation_metrics", default=None)


@contextmanager
def job_metrics(store, job_id, node):
    token = _active.set((store, job_id, node))
    try:
        yield
    finally:
        _active.reset(token)


def set_metric_stage(node):
    if context := _active.get():
        _active.set((*context[:2], node))


def record_operation(operation, service, *, elapsed=0, wait=0, cached=False, error=None):
    if context := _active.get():
        store, job_id, node = context
        store.event(job_id, node=node, status="running", kind="operation",
                    operation=operation, service=service,
                    operation_duration_s=round(elapsed, 3), wait_s=round(wait, 3),
                    cache_hit=cached, error_type=type(error).__name__ if error else None,
                    message=f"{operation}：{'复用已有结果' if cached else '失败' if error else '完成'}（{elapsed:.1f}秒）")


@contextmanager
def measure(operation, service):
    started = time.monotonic()
    try:
        yield
    except BaseException as error:
        record_operation(operation, service, elapsed=time.monotonic()-started, error=error)
        raise
    else:
        record_operation(operation, service, elapsed=time.monotonic()-started)
