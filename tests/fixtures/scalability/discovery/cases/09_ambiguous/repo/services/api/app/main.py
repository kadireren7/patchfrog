from fastapi import FastAPI

from app.core import router

app = FastAPI()
app.include_router(router)
