from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query

from agent_observatory.api.deps import DB, CurrentPrincipal
from agent_observatory.services.stats import token_usage

router = APIRouter(prefix="/api/stats", tags=["stats"])


@router.get("/token-usage")
async def get_token_usage(
    db: DB,
    principal: CurrentPrincipal,
    range_name: Literal["day", "week", "month"] = Query("day", alias="range"),
):
    """Return aggregate token usage for the selected UTC time window."""
    return await token_usage(db, range_name)
