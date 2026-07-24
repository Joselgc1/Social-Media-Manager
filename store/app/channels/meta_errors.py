"""Delivery outcome errors for Meta channel senders."""


class MetaSendError(RuntimeError):
    """A Meta send failure with a known delivery outcome."""

    def __init__(self, message: str, *, retryable: bool, delivery_known: bool = True):
        super().__init__(message)
        self.retryable = retryable
        self.delivery_known = delivery_known
