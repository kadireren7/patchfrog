from typing import Annotated

from fastapi import Depends, Header, Security
from fastapi.security import OAuth2PasswordBearer

oauth2 = OAuth2PasswordBearer(tokenUrl="token")


def get_db():
    db = object()
    try:
        yield db
    finally:
        pass


async def get_token(token: str = Depends(oauth2)):
    return token


async def get_current_user(token: Annotated[str, Depends(get_token)], db=Depends(get_db)):
    return {"token": token}


def verify_key(x_key: str = Header()):
    return x_key


class Pagination:
    def __init__(self, skip: int = 0, limit: int = 10):
        self.skip = skip
        self.limit = limit


CurrentUser = Annotated[dict, Depends(get_current_user)]


def require_scope(user=Security(get_current_user, scopes=["admin"])):
    return user
