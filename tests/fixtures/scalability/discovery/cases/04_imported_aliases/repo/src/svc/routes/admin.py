from fastapi import APIRouter

router = APIRouter()


@router.delete("/cache")
async def clear_cache():
    return None
