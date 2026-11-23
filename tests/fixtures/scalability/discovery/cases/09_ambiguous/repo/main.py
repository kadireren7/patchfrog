import os

from fastapi import APIRouter, FastAPI

app = FastAPI()

if os.environ.get("V2"):
    router = APIRouter(prefix="/v2")
else:
    router = APIRouter(prefix="/v1")


@router.get("/items")
def items():
    return []


try:
    from fast_json import dumps
except ImportError:
    dumps = None

app.include_router(router)
