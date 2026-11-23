from typing import Annotated

from external_auth import get_api_client
from fastapi import APIRouter, Depends, FastAPI

from app.deps import CurrentUser, Pagination, get_db, require_scope, verify_key

app = FastAPI(dependencies=[Depends(verify_key)])
router = APIRouter(prefix="/r", dependencies=[Depends(get_db)])


@router.get("/me")
async def me(user: CurrentUser):
    return user


@router.get("/items", dependencies=[Depends(require_scope)])
def items(page: Pagination = Depends(), client=Depends(get_api_client)):
    return []


@app.get("/plain")
def plain(page: Annotated[Pagination, Depends()]):
    return {}


app.include_router(router, prefix="/v1", dependencies=[Depends(get_db)])
