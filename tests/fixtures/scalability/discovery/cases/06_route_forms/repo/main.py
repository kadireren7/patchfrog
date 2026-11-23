from fastapi import APIRouter, FastAPI, WebSocket

BASE = "/files"

app = FastAPI()
router = APIRouter()


@router.api_route("/multi", methods=["GET", "post"])
async def multi():
    return {}


@router.api_route("/default")
def default_get():
    return {}


def health():
    return {"ok": True}


async def stream(ws: WebSocket):
    await ws.accept()


router.add_api_route("/health", health)
router.add_api_route("/status", health, methods=["HEAD", "GET"])
app.router.add_api_route(BASE + "/list", health)
app.add_api_websocket_route("/ws", stream)


@app.websocket("/live")
async def live(ws: WebSocket):
    await ws.accept()


app.get("/callform")(health)

app.include_router(router, prefix="/r")
