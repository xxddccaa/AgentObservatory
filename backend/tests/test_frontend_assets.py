from __future__ import annotations

from httpx import AsyncClient


async def test_favicon_is_served_as_svg(client: AsyncClient) -> None:
    response = await client.get("/favicon.svg")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert response.text.lstrip().startswith("<svg")
