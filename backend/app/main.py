from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.database import Base, engine
from app import models  # noqa: F401 — register ORM models before create_all.
from app.routes import router
from app.chat_routes import chat_router
from app.document_routes import document_router
from app.memory_routes import hindsight_error_handler, memory_router
from app.services.hindsight import HindsightError

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)
app.include_router(memory_router)
app.include_router(chat_router)
app.include_router(document_router)
app.add_exception_handler(HindsightError, hindsight_error_handler)
