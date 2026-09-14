"""Exception types shared across the listener.

Both error types propagate — ``router`` raises deliberately rather than
swallowing, because swallowing an error loses the approval permanently (a fresh
consumer ID starts at the latest offset and cannot replay history).  The
pipeline's ``retry_count: 3`` and ``failure_mode: CONTINUE`` settings then
handle the raise: three retries, then the event is written to
``failed_events.log`` and the pipeline continues.

The distinction between ``DataHubTransientError`` and ``DataHubError`` is kept
for log forensics (operators can grep for the subclass name to find retriable
vs. permanent failures), but nothing in the current codebase branches on it at
runtime.
"""


class DataHubError(RuntimeError):
    """A permanent failure. Retrying will not help."""


class DataHubTransientError(DataHubError):
    """A temporary failure — network, timeout, or 5xx. Retrying may help."""


class MissingFieldError(DataHubError):
    """A required form field was absent or empty."""


class NotificationError(DataHubError):
    """A notification could not be sent, and retrying will not help.

    Subclasses the DataHub error types deliberately: nothing branches on the
    class at runtime, but inheriting means the pipeline's retry and
    failed-events behaviour applies to notifications exactly as it does to
    metadata writes, with no extra wiring.
    """


class NotificationTransientError(DataHubTransientError):
    """A notification failed for a temporary reason -- network, timeout, 5xx."""
