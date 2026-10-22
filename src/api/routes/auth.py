from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from src.services.web_auth_service import SESSION_COOKIE


router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(max_length=256)
    password: str = Field(max_length=4096)


@router.post("/status")
async def login(payload: LoginRequest, request: Request, response: Response):
    sessions = request.app.state.web_auth_service
    token = sessions.login(payload.username, payload.password)
    if token is None:
        raise HTTPException(status_code=401, detail="认证失败")
    sessions.revoke(request.cookies.get(SESSION_COOKIE))
    settings = sessions.settings_provider()
    response.set_cookie(
        SESSION_COOKIE, token, httponly=True, samesite="strict", path="/",
        secure=settings.web_cookie_secure or request.url.scheme == "https",
        max_age=settings.web_session_ttl_seconds,
    )
    response.headers["Cache-Control"] = "no-store"
    return {"authenticated": True, "username": payload.username}


@router.get("/status")
async def auth_status(request: Request, response: Response):
    session = request.app.state.web_auth_service.validate(request.cookies.get(SESSION_COOKIE))
    if session is None:
        raise HTTPException(status_code=401, detail="请先登录")
    response.headers["Cache-Control"] = "no-store"
    return {"authenticated": True, "username": session.username}


@router.post("/logout")
async def logout(request: Request, response: Response):
    request.app.state.web_auth_service.revoke(request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="strict")
    response.headers["Cache-Control"] = "no-store"
    return {"authenticated": False}
