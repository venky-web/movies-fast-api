from fastapi import APIRouter

from app.api.deps import ReposDep, UserDep
from app.core.errors import NOT_FOUND_RESPONSE
from app.schemas.user import User

router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("", response_model=list[User], summary="List users", response_description="The mock profiles")
async def list_users(repos: ReposDep):
    """The four mock profiles. Pick an `id` to use as `userId`."""
    return await repos.users.list_all()


@router.get(
    "/{user_id}",
    response_model=User,
    summary="Get a user",
    response_description="The profile",
    responses=NOT_FOUND_RESPONSE,
)
async def get_user(user: UserDep):
    """One mock profile."""
    return user
