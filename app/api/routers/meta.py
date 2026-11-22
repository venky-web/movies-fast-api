from fastapi import APIRouter, Request

router = APIRouter(tags=["meta"])


@router.get("/health", include_in_schema=False)
async def health(request: Request) -> dict[str, str]:
    return {"status": "ok", "storage": request.app.state.settings.storage_backend}
