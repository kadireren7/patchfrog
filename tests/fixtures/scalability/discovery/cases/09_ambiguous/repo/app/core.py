from fastapi import APIRouter

router = APIRouter()


@router.get("/top")
def top():
    return {}
