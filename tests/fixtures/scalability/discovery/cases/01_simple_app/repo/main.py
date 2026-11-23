from fastapi import FastAPI

app = FastAPI(title="simple")


@app.get("/")
async def root():
    return {"ok": True}


@app.post("/items")
def create_item(item: dict):
    return item


@app.put("/items/{item_id}")
async def replace_item(item_id: int):
    return {}


@app.patch("/items/{item_id}")
def update_item(item_id: int):
    return {}


@app.delete("/items/{item_id}")
async def delete_item(item_id: int):
    return None


@app.head("/items")
def head_items():
    return None


@app.options("/items")
def options_items():
    return None
