from fastapi import HTTPException


class GatewayError(HTTPException):
    """Error rendered in OpenAI's error format on the /v1 API."""

    def __init__(self, status: int, code: str, message: str, type_: str = "invalid_request_error",
                 headers: dict[str, str] | None = None):
        super().__init__(status_code=status, headers=headers, detail={"openai": {
            "message": message, "type": type_, "param": None, "code": code}})
