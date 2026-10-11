"""Context-local stage ownership and cooperative stop checks."""

import time
from contextlib import contextmanager
from contextvars import ContextVar

_owner = ContextVar("pipeline_owner", default=None)
_resources = ContextVar("pipeline_resources", default=frozenset())


class OwnershipLost(RuntimeError):
    code = "PIPELINE_OWNERSHIP_LOST"


class PipelinePaused(RuntimeError):
    code = "PIPELINE_PAUSED"


class TemporaryBudgetExceeded(RuntimeError):
    code = "PIPELINE_TEMP_BUDGET_EXCEEDED"


@contextmanager
def stage_owner(owner):
    token = _owner.set(owner)
    try:
        yield
    finally:
        _owner.reset(token)


def assert_owner(connection=None):
    owner = _owner.get()
    if owner is not None:
        owner.assert_live(connection)


def before_operation():
    """Do not start new operations after a pause, stop, or exhausted budget."""
    owner = _owner.get()
    if owner is not None:
        owner.before_operation()


def request_succeeded():
    owner = _owner.get()
    if owner is not None:
        owner.request_succeeded()


@contextmanager
def embedding_slot(video):
    """Serialize native and unified summary indexing, including backfill."""
    if not video.pipeline_enabled or "embedding" in _resources.get():
        yield
        return
    from .config import load_config
    from .database import connect_database
    from .queue_control import read_control
    from .video_store import library_id

    with connect_database(load_config(), register_pgvector=False) as guard:
        guard.autocommit = True
        while not guard.execute(
            "SELECT pg_try_advisory_lock(hashtext(%s))",
            ("rag-embedding:" + library_id(video),),
        ).fetchone()[0]:
            before_operation()
            if read_control(video).paused:
                raise PipelinePaused()
            time.sleep(0.2)
        before_operation()
        if read_control(video).paused:
            raise PipelinePaused()
        token = _resources.set(_resources.get() | {"embedding"})
        try:
            yield
        finally:
            _resources.reset(token)


@contextmanager
def llm_slot(video):
    """Share one CCR slot with inline extraction and historical summary backfill."""
    if not video.pipeline_enabled:
        yield
        return
    from .config import load_config
    from .database import connect_database
    from .queue_control import read_control
    from .video_store import library_id

    with connect_database(load_config(), register_pgvector=False) as guard:
        guard.autocommit = True
        while not guard.execute(
            "SELECT pg_try_advisory_lock(hashtext(%s))",
            ("rag-ccr:" + library_id(video),),
        ).fetchone()[0]:
            before_operation()
            if read_control(video).paused:
                raise PipelinePaused()
            time.sleep(0.2)
        before_operation()
        if read_control(video).paused:
            raise PipelinePaused()
        yield
