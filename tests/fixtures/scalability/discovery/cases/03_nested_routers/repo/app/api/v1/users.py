from fastapi import APIRouter

router = APIRouter(prefix="/users")


@router.get("/")
async def list_users():
    return []


@router.get("/me")
async def me():
    return {}
