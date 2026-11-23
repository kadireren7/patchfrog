from fastapi import APIRouter, FastAPI
from missing_pkg.routes import external_router

from .not_here import local_router

app = FastAPI()
router = APIRouter()
VERSION = get_version()

ROUTERS = [router]
for r in ROUTERS:
    app.include_router(r)

app.include_router(external_router)
app.include_router(local_router)


@router.get(f"/v{VERSION}/items")
def versioned():
    return []


@app.get("/known")
def known():
    return {}


def register(target):
    @target.get("/plugin")
    def plugin():
        return {}


method = "GET"
app.add_api_route("/dyn", known, methods=[method])
app.include_router(router, prefix="/p" + VERSION)
