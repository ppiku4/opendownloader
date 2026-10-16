"""Run blocking work off the UI thread and deliver the result back on it."""
from __future__ import annotations

import threading
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal


class _Relay(QObject):
    done = Signal(object, object)  # result, exception


_keep: set[_Relay] = set()


def run_async(fn: Callable[[], Any], callback: Callable[[Any, BaseException | None], None]) -> None:
    relay = _Relay()
    _keep.add(relay)

    def deliver(result: Any, exc: BaseException | None) -> None:
        _keep.discard(relay)
        callback(result, exc)

    relay.done.connect(deliver)

    def work() -> None:
        try:
            res = fn()
        except BaseException as e:  # noqa: BLE001
            relay.done.emit(None, e)
            return
        relay.done.emit(res, None)

    threading.Thread(target=work, daemon=True).start()
