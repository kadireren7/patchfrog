from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles


class VersionedRouter(APIRouter):
    pass


app = FastAPI()
sub = FastAPI()
custom = VersionedRouter(prefix="/custom")
listed = APIRouter(routes=[APIRoute("/listed", lambda: None)])


@custom.get("/x")
def custom_x():
    return {}


@sub.get("/inner")
def inner():
    return {}


app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/sub", sub)
app.add_route("/legacy", lambda request: None)
app.include_router(custom)
app.include_router(listed)
