from fastapi import APIRouter
from app.api.auth import router as auth_router
from app.api.admin import router as admin_router
from app.api.channels import router as channel_router
from app.api.library import router as library_router
from app.api.media import router as episodes_router
from app.api.stream import router as stream_router

api_router = APIRouter(prefix="/api")

api_router.include_router(auth_router)
api_router.include_router(admin_router)
api_router.include_router(channel_router)
api_router.include_router(library_router)
api_router.include_router(episodes_router)
api_router.include_router(stream_router)
