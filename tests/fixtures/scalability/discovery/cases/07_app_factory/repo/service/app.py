from fastapi import FastAPI

from service.api import router


def create_app() -> FastAPI:
    application = FastAPI()
    application.include_router(router)

    @application.get("/")
    async def index():
        return {}

    return application


app = create_app()
