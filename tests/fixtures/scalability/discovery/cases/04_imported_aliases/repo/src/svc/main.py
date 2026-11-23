import fastapi as fa

from svc import routes
from svc.routes import users as user_routes
from svc.routes.orders import router as orders_router

app = fa.FastAPI()
public = app

public.include_router(user_routes.router, prefix="/users")
app.include_router(orders_router)
app.include_router(routes.admin_router, prefix="/admin")
