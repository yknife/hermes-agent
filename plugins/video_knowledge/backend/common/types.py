from typing import Literal, TypedDict


class ServiceStatus(TypedDict):
    name: str
    status: Literal["ok", "error"]
