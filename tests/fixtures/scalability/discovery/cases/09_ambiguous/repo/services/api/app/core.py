from fastapi import APIRouter

router = APIRouter()


@router.get("/svc")
def svc():
    return {}
