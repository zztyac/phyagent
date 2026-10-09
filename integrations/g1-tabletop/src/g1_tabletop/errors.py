"""Structured execution failures; uncertainty is never silently converted to success."""


class ExecutionError(Exception):
    def __init__(self, code: str, message: str, *, status: int = 409, uncertain: bool = False):
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.status, self.uncertain = code, message, status, uncertain

    def payload(self) -> dict:
        return {"code": self.code, "message": self.message, "retryable": False}
