from fastapi import APIRouter

router = APIRouter(

@router.get("/never")
def never():
    return {}
