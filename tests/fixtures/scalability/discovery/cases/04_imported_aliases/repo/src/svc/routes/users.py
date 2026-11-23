from fastapi import APIRouter as Router

router = Router()
alias = router


@alias.get("/{user_id}")
async def get_user(user_id: int):
    return {}
