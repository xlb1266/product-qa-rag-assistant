"""健康检查接口：用于确认后端服务是否正常运行。"""

from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}
