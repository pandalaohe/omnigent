"""Resolve the server telemetry setting and its deployment overrides."""

from __future__ import annotations

import os
from collections.abc import Mapping

from pydantic import BaseModel, StrictBool
from starlette.types import ASGIApp, Receive, Scope, Send

from omnigent.server.sharing_settings import read_telemetry_enabled_override
from omnigent.telemetry.client import (
    ServerUsageTelemetry,
    _config_telemetry_disabled,
    config_disables_telemetry,
)
from omnigent.telemetry.environment import telemetry_override


class TelemetryStatus(BaseModel):
    enabled: bool
    locked: bool
    source: str


class TelemetrySettingsUpdate(BaseModel):
    telemetry_enabled: StrictBool


class TelemetrySettings:
    def __init__(self, config: Mapping[str, object]) -> None:
        self._disabled_by_config = (
            config_disables_telemetry(config) or _config_telemetry_disabled()
        )

    def status(self) -> TelemetryStatus:
        override = telemetry_override(os.environ, disabled_by_config=self._disabled_by_config)
        if override is not None:
            enabled, source = override
            return TelemetryStatus(enabled=enabled, locked=True, source=source)
        stored = read_telemetry_enabled_override()
        return TelemetryStatus(
            enabled=stored is True, locked=False, source="default" if stored is None else "setting"
        )

    def enabled(self) -> bool:
        return self.status().enabled


def configure_server_boot_telemetry(config: Mapping[str, object]) -> bool:
    """Resolve boot telemetry on/off and pin server OTel before stores initialize."""
    from omnigent.runtime import telemetry

    enabled = TelemetrySettings(config).enabled()
    telemetry.init(os.environ.get("OTEL_SERVICE_NAME") or "omni-server", server_opted_in=enabled)
    return enabled


class UsageTelemetryMiddleware:
    def __init__(self, app: ASGIApp, telemetry: ServerUsageTelemetry) -> None:
        self._app = app
        self._telemetry = telemetry

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        with self._telemetry.scope():
            await self._app(scope, receive, send)
