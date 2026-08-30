"""API 路由聚合。"""

from fastapi import APIRouter

from src.api.routes import chat, documents, evaluation, knowledge_bases

api_router = APIRouter()
api_router.include_router(documents.router)
api_router.include_router(chat.router)
api_router.include_router(knowledge_bases.router)
api_router.include_router(evaluation.router)
