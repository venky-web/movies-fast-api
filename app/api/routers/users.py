from fastapi import APIRouter

from app.api.deps import ReposDep, UserDep
from app.schemas.user import User

router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("", response_model=list[User])
async def list_users(repos: ReposDep):
    return await repos.users.list_all()


@router.get("/{user_id}", response_model=User)
async def get_user(user: UserDep):
    return user
