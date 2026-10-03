"""Startup gate, independent of the library that may require conversion."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict
from typing import Literal


class UpgradeConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed: Literal[True]


def create_upgrade_router(controller,on_ready):
    router=APIRouter()
    @router.get("/api/startup")
    def status(): return controller.snapshot()
    @router.post("/api/startup/upgrade")
    async def upgrade(body: UpgradeConfirm):
        if not controller.snapshot()["can_start"]:
            raise HTTPException(409,"图库正在处理或已经准备好")
        return await controller.start(on_ready)
    return router
