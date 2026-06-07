from typing import Any

from pydantic import BaseModel


class ApiResponse(BaseModel):
    code: int = 0
    msg: str = "success"
    data: Any = None


def success_response(data: Any) -> dict:
    return {"code": 0, "msg": "success", "data": data}


def error_response(code: int, msg: str, data: Any = None) -> dict:
    return {"code": code, "msg": msg, "data": data}

