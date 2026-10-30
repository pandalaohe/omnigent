"""``/api/version`` reports a stamped distribution version next to the package version."""

from __future__ import annotations

import httpx
import pytest

import omnigent.distribution as distribution
from omnigent.version import VERSION


@pytest.mark.asyncio
async def test_version_adds_the_distribution_when_stamped(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(distribution, "DISTRIBUTION_VERSION", "0.21.0-wukong.12")

    resp = await client.get("/api/version")

    assert resp.status_code == 200
    assert resp.json() == {"version": VERSION, "distribution": "0.21.0-wukong.12"}


@pytest.mark.asyncio
async def test_version_has_no_distribution_without_a_stamp(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/version")

    assert resp.json() == {"version": VERSION}
