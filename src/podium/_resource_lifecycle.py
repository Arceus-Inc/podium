"""Small shared lifecycle primitive for Podium's two engine connection owners."""

from __future__ import annotations

from collections.abc import Callable

CloseCallback = Callable[[], object]


def close_owned_resources(
    *closers: CloseCallback, primary: BaseException | None = None
) -> None:
    """Attempt every close and preserve construction plus cleanup failures."""
    failures: list[BaseException] = []
    for close in closers:
        try:
            close()
        except BaseException as error:
            failures.append(error)

    if primary is not None:
        if failures:
            raise BaseExceptionGroup(
                "resource construction and cleanup failed", [primary, *failures]
            ) from None
        return
    if len(failures) == 1:
        raise failures[0]
    if failures:
        raise BaseExceptionGroup("resource cleanup failed", failures)


__all__ = ["close_owned_resources"]
