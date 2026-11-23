from fastapi import APIRouter, Depends, FastAPI

from db import get_conn, get_legacy, get_session

app = FastAPI()
router = APIRouter(prefix="/v1", dependencies=[Depends(get_session)])


@router.get("/a")
async def a():
    return {}


@router.get("/b")
async def b(s=Depends(get_legacy), c=Depends(get_conn)):
    return {}


app.include_router(router)
