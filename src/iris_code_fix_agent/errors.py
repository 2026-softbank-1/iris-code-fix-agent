class RepairError(Exception):
    """Public safe error: never include source URLs or provider response bodies."""

    def __init__(self, code: str, message: str, http_status: int = 422):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
