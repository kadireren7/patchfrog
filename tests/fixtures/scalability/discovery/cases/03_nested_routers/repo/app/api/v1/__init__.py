from fastapi import APIRouter

from . import users
from .items import router as items_router

v1_router = APIRouter()
v1_router.include_router(users.router)
v1_router.include_router(items_router, prefix="/items")
