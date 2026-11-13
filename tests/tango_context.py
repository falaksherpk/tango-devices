"""PyTango's test contexts, unable to leave a device server behind (Part 4, Ch43).

PyTango's ``__enter__`` starts the device server and then pings it. If the ping
fails, ``__enter__`` raises, ``__exit__`` never runs, and with ``process=True``
the server process stays alive. At exit, the interpreter's multiprocessing hook
waits for that process, so pytest never ends: CI job #335 ran until GitLab's
one-hour timeout (Ch42, Ch43).

These subclasses keep PyTango's names and behaviour, and add two things to
``__enter__``:

- a failed start kills its server process and runs PyTango's own ``stop()``
  (the test database and the environment variables), then raises: it fails fast;
- a start refused by cppTango's client-side reconnection limit
  (``API_CantConnectToDevice``, "the connection request was delayed", after an
  earlier failed attempt in the same second) is retried once, with a fresh
  server, after that second has passed, and reported as a warning. Seen once in
  CI (#335), not reproduced in 70 sandbox runs; the refusal is the harness
  failing to reach a server it just started, not a device behaviour.
"""

import contextlib
import time
import warnings

import tango
from tango import test_context as _pytango

RETRYABLE_REASON = "API_CantConnectToDevice"
RETRY_DELAY_S = 1.1  # past cppTango's 1000 ms reconnection limit


def _refused(exc):
    return isinstance(exc, tango.DevFailed) and any(
        getattr(err, "reason", None) == RETRYABLE_REASON for err in exc.args
    )


def _abandon(ctx):
    """Kill the server a failed start left, then let PyTango clean up after it."""
    thread = ctx.thread
    if getattr(ctx, "_process", False) and thread.is_alive():
        thread.kill()
        thread.join(5)
    # stop()'s finally deletes the test database and restores the environment. Its
    # own error is dropped: the start already failed, and that error is the one raised.
    with contextlib.suppress(Exception):
        ctx.stop()


class _FailFastEnter:
    def __init__(self, *args, **kwargs):
        self._ch43_args = (args, kwargs)
        super().__init__(*args, **kwargs)

    def __enter__(self):
        try:
            return super().__enter__()
        except BaseException as exc:
            _abandon(self)
            if not _refused(exc):
                raise
            warnings.warn(
                f"{type(self).__name__}: device server start refused "
                f"({RETRYABLE_REASON}); retrying once with a fresh server after "
                f"{RETRY_DELAY_S} s",
                stacklevel=2,
            )
        time.sleep(RETRY_DELAY_S)
        args, kwargs = self._ch43_args
        super().__init__(*args, **kwargs)  # a fresh server thread or process
        try:
            return super().__enter__()
        except BaseException:
            _abandon(self)
            raise


class DeviceTestContext(_FailFastEnter, _pytango.DeviceTestContext):
    """PyTango's DeviceTestContext; a failed start leaves no server (see above)."""


class MultiDeviceTestContext(_FailFastEnter, _pytango.MultiDeviceTestContext):
    """PyTango's MultiDeviceTestContext; a failed start leaves no server."""
