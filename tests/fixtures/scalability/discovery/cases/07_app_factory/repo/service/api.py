from fastapi import APIRouter

router = APIRouter(prefix="/api")


@router.get("/ping")
def ping():
    return "pong"
