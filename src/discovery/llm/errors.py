class LLMError(RuntimeError):
    """A safe, user-facing failure; provider payloads never enter the message."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)
