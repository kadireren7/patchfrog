import fastapi

api = fastapi.FastAPI()


@api.get("/status")
async def status():
    return {}
