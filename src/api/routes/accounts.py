"""
闲鱼账号管理路由
"""
import asyncio
import json
import os
import re
import tempfile
import aiofiles
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List
from pathlib import Path
from fastapi.responses import JSONResponse
from src.infrastructure.config.env_manager import env_manager
from src.infrastructure.paths import safe_child_path
from src.services.account_login_service import account_login_service


router = APIRouter(prefix="/api/accounts", tags=["accounts"])

ACCOUNT_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,50}$")


class AccountCreate(BaseModel):
    name: str
    content: str


class AccountUpdate(BaseModel):
    content: str


class AccountLoginCreate(BaseModel):
    name: str


def _strip_quotes(value: str) -> str:
    if not value:
        return value
    if value.startswith(("\"", "'")) and value.endswith(("\"", "'")):
        return value[1:-1]
    return value


def _state_dir() -> str:
    raw = env_manager.get_value("ACCOUNT_STATE_DIR", "state") or "state"
    return _strip_quotes(raw.strip())


def _ensure_state_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _validate_name(name: str) -> str:
    trimmed = name.strip()
    if not trimmed or not ACCOUNT_NAME_RE.fullmatch(trimmed):
        raise HTTPException(status_code=400, detail="账号名称只能包含字母、数字、下划线或短横线。")
    return trimmed


def _account_path(name: str) -> str:
    try:
        return safe_child_path(_state_dir(), f"{_validate_name(name)}.json")
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的账号文件路径")


def _validate_json(content: str) -> None:
    try:
        json.loads(content)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="提供的内容不是有效的JSON格式。")


@router.get("", response_model=List[dict])
async def list_accounts():
    state_dir = _state_dir()
    if not os.path.isdir(state_dir):
        return []
    files = [f for f in os.listdir(state_dir) if f.endswith(".json")]
    accounts = []
    for filename in sorted(files):
        name = filename[:-5]
        accounts.append({
            "name": name,
            "path": os.path.join(state_dir, filename),
        })
    return accounts


@router.post("/qr-login", response_model=dict)
async def start_qr_login(data: AccountLoginCreate):
    name = _validate_name(data.name)
    path = Path(_account_path(name)).resolve()
    if path.exists():
        raise HTTPException(status_code=409, detail="账号已存在，请使用其他账号名称。")
    try:
        result = account_login_service.start(name, path)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.get("/qr-login/{session_id}", response_model=dict)
async def get_qr_login(session_id: str):
    try:
        result = account_login_service.get(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="扫码会话不存在或已过期，请重新生成二维码。")
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.delete("/qr-login/{session_id}", response_model=dict)
async def cancel_qr_login(session_id: str):
    try:
        account_login_service.cancel(session_id)
    except KeyError:
        pass
    return {"message": "扫码会话已关闭"}


@router.get("/{name}", response_model=dict)
async def get_account(name: str):
    account_name = _validate_name(name)
    path = _account_path(account_name)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="账号不存在")
    async with aiofiles.open(path, "r", encoding="utf-8") as f:
        content = await f.read()
    return {"name": account_name, "path": path, "content": content}


def _atomic_write_account(path: str, content: str, *, exclusive: bool = False) -> None:
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    if exclusive and os.path.exists(path):
        raise FileExistsError(f"账号文件已存在: {path}")
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=parent, prefix=".acc-", suffix=".tmp", delete=False
        ) as f:
            tmp = f.name
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        if exclusive:
            try:
                os.link(tmp, path)
            except (OSError, NotImplementedError, AttributeError):
                if os.path.exists(path):
                    raise FileExistsError(f"账号文件已存在: {path}")
                os.replace(tmp, path)
        else:
            os.replace(tmp, path)
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


@router.post("", response_model=dict)
async def create_account(data: AccountCreate):
    account_name = _validate_name(data.name)
    _validate_json(data.content)
    state_dir = _state_dir()
    _ensure_state_dir(state_dir)
    path = _account_path(account_name)
    if os.path.exists(path):
        raise HTTPException(status_code=409, detail="账号已存在")
    try:
        await asyncio.to_thread(_atomic_write_account, path, data.content, exclusive=True)
    except FileExistsError:
        raise HTTPException(status_code=409, detail="账号已存在")
    return {"message": "账号已添加", "name": account_name, "path": path}


@router.put("/{name}", response_model=dict)
async def update_account(name: str, data: AccountUpdate):
    account_name = _validate_name(name)
    _validate_json(data.content)
    state_dir = _state_dir()
    _ensure_state_dir(state_dir)
    path = _account_path(account_name)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="账号不存在")
    await asyncio.to_thread(_atomic_write_account, path, data.content, exclusive=False)
    return {"message": "账号已更新", "name": account_name, "path": path}


@router.delete("/{name}", response_model=dict)
async def delete_account(name: str):
    account_name = _validate_name(name)
    path = _account_path(account_name)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="账号不存在")
    os.remove(path)
    return {"message": "账号已删除"}
