class GatewayError(Exception):
    def __init__(self, message, code="upstream_error", status=502, *, ambiguous=False, retryable=False):
        super().__init__(message)
        self.code, self.status = code, status
        self.ambiguous, self.retryable = ambiguous, retryable

