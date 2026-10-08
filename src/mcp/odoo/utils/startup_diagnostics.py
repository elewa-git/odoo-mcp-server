"""Bounded startup diagnostics that never serialize arbitrary exception text."""

from __future__ import annotations

import re
from enum import Enum


class CheckFailure(Enum):
    SDK_VERSION = "installed SDK does not match this server implementation"
    DATABASE = "active database does not match the intended database"
    PAGINATION = "tool discovery pagination exceeded its bounds"
    MISSING_TOOLS = "required read tools are missing"
    TOOL_ERROR = "read-only diagnostic tool returned an error; response body withheld"
    PING = "ping did not confirm the intended database"


class StartupCheckError(RuntimeError):
    """Only controlled verifier failures may include their descriptive message."""

    def __init__(self, failure: CheckFailure):
        self.failure = failure
        super().__init__(failure.value)


def startup_failure(stage: str, error: BaseException, stderr: str = "") -> str:
    """Expose nested causes without copying credentials or HTTP response bodies.

    Redacting known keys alone is insufficient: response bodies can contain
    unrelated business data. Only allowlisted structural details leave here.
    """
    causes: list[str] = []
    seen: set[int] = set()

    def visit(exc: BaseException) -> None:
        if id(exc) in seen:
            return
        seen.add(id(exc))
        if isinstance(exc, BaseExceptionGroup):
            for child in exc.exceptions:
                visit(child)
            return
        if exc.__cause__ is not None:
            visit(exc.__cause__)
        elif exc.__context__ is not None and not exc.__suppress_context__:
            visit(exc.__context__)
        detail = type(exc).__name__
        if isinstance(exc, StartupCheckError):
            detail += f": {exc.failure.value}"
        elif isinstance(exc, ImportError) and exc.name and re.fullmatch(r"[A-Za-z_]\w*(?:\.\w+)*", exc.name):
            detail += f": missing or incompatible module {exc.name}"
        elif isinstance(exc, OSError) and isinstance(exc.errno, int):
            detail += f": OS error {exc.errno}"
        causes.append(detail)

    visit(error)
    # Child import failures otherwise disappear inside the client's ExceptionGroup.
    # Never echo traceback source lines, generic messages, or HTTP bodies.
    for module in re.findall(r"ModuleNotFoundError: No module named ['\"]([A-Za-z_]\w*(?:\.\w+)*)['\"]", stderr):
        causes.append(f"ModuleNotFoundError: missing module {module}")
    details = "; ".join(dict.fromkeys(causes))
    return f"Verification failed during {stage}: {details}"[:2000]
