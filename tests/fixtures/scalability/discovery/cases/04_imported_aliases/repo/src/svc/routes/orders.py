from fastapi import routing

router = routing.APIRouter(prefix="/orders")


@router.post("")
def place_order():
    return {}
