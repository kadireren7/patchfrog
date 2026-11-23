from fastapi import APIRouter, FastAPI

API_PREFIX = "/api"
V1 = API_PREFIX + "/v1"

router = APIRouter(prefix="/items", tags=["items"])


@router.get("")
async def list_items():
    return []


@router.get("/{item_id}")
async def get_item(item_id: int):
    return {}


@router.post("/")
def create_item():
    return {}


app = FastAPI()
app.include_router(router, prefix=V1)
