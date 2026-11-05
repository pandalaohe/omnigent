"""One server-wide public price source, persisted and refreshed daily."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal, Self
from urllib.parse import parse_qsl, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.exc import SQLAlchemyError

from omnigent.diagnostics import redact_url
from omnigent.llms.context_window import ModelPricing, fetch_model_pricing_with_provider
from omnigent.llms.price_source import READERS, PriceMap, install_price_map
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.stores.usage_hours_store import UsageHoursStore
from omnigent.stores.usage_snapshot_store import UsageSnapshotStore

_logger = logging.getLogger(__name__)
_MAX_BYTES = 10 * 1024 * 1024
_DAY = 86400
_CREDENTIAL_QUERY_NAME = re.compile(
    r"(?:key|token|secret|password|passwd|signature|credential|authorization|auth|sig)$"
)
_PRESETS = {
    "litellm": "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json",
    "openrouter": "https://openrouter.ai/api/v1/models",
    "modelsdev": "https://models.dev/api.json",
}


class PriceSourceSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["builtin", "litellm", "openrouter", "modelsdev", "custom"] = "builtin"
    format: Literal["litellm", "openrouter", "modelsdev"] | None = None
    url: str | None = Field(default=None, max_length=2048)

    @model_validator(mode="after")
    def validate_selection(self) -> Self:
        if self.source == "custom":
            if self.format is None or self.url is None:
                raise ValueError("Choose a format and an HTTPS URL")
            try:
                url = urlsplit(self.url)
                valid = (
                    url.scheme == "https"
                    and bool(url.hostname)
                    and url.username is None
                    and url.password is None
                    and not url.fragment
                    and (url.port is None or 0 < url.port < 65536)
                    and not any(char.isspace() for char in self.url)
                )
            except ValueError:
                raise ValueError("Use an HTTPS URL without credentials or a fragment") from None
            if not valid:
                raise ValueError("Use an HTTPS URL without credentials or a fragment")
            if any(
                _CREDENTIAL_QUERY_NAME.search(re.sub(r"[^a-z0-9]", "", name.lower()))
                for name, _ in parse_qsl(url.query, keep_blank_values=True)
            ):
                raise ValueError("Price source URLs cannot contain credential query parameters")
        elif self.url is not None or self.format is not None:
            raise ValueError("A built-in choice does not take a URL or format")
        return self

    def location(self) -> tuple[str, str] | None:
        if self.source == "builtin":
            return None
        if self.source == "custom":
            assert self.url is not None and self.format is not None
            return self.url, self.format
        return _PRESETS[self.source], self.source


async def fetch_prices(selection: PriceSourceSelection) -> PriceMap:
    """Fetch a bounded JSON document without redirects, proxy or auth inheritance."""
    location = selection.location()
    if location is None:
        return PriceMap({})
    url, format_name = location
    try:
        async with asyncio.timeout(30):
            # The transport has no client INFO URL logging, redirects or inherited auth.
            async with httpx.AsyncHTTPTransport(trust_env=False) as transport:
                request = httpx.Request(
                    "GET", url, extensions={"timeout": httpx.Timeout(30).as_dict()}
                )
                response = await transport.handle_async_request(request)
                try:
                    if response.status_code != 200:
                        raise ValueError(f"Price source returned HTTP {response.status_code}")
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > _MAX_BYTES:
                            raise ValueError("Price source exceeds 10 MB")
                finally:
                    await response.aclose()
        payload = json.loads(content)
    except (httpx.HTTPError, TimeoutError):
        raise ValueError("Price source could not be reached within 30 seconds") from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError("Price source did not return valid JSON") from None
    prices = READERS[format_name](payload)
    if not prices:
        raise ValueError("Price source contains no valid input and output prices in this format")
    return PriceMap(prices)


class UsagePriceSource:
    def __init__(
        self, snapshots: UsageSnapshotStore, hours: UsageHoursStore, cache_dir: Path
    ) -> None:
        self.snapshots = snapshots
        self.hours = hours
        self.cache_path = cache_dir / "usage-price-source.json"
        self.selection = PriceSourceSelection()
        self.prices: PriceMap | None = None
        self.fetched_at: float | None = None
        self.last_attempt_at: float | None = None
        self.error: str | None = None
        self.failures: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()

    def load(self) -> None:
        """Restore a matching cache before the refresh task needs the network."""
        install_price_map(None)
        try:
            records = self.snapshots.read(RESERVED_USER_LOCAL)
            saved = records.get("usage.price_source", {})
            entries = records.get("usage.price_source_failures", {}).get("entries", [])
            self.failures = (
                [
                    entry
                    for entry in entries
                    if isinstance(entry, dict)
                    and isinstance(entry.get("at"), (int, float))
                    and math.isfinite(entry["at"])
                    and isinstance(entry.get("error"), str)
                ][-16:]
                if isinstance(entries, list)
                else []
            )
            self.selection = PriceSourceSelection.model_validate(saved)
            if self.selection.source == "builtin":
                return
            if self.cache_path.stat().st_size > _MAX_BYTES:
                raise ValueError("Price cache exceeds 10 MB")
            cache = json.loads(self.cache_path.read_bytes())
            if cache.get("selection") != self.selection.model_dump():
                return
            prices = {model: ModelPricing(**price) for model, price in cache["prices"].items()}
            # Revalidate the disk data with the same rules as a fetched map.
            normalized = READERS["litellm"](
                {
                    model: {
                        "input_cost_per_token": price.input_per_token,
                        "output_cost_per_token": price.output_per_token,
                        "cache_read_input_token_cost": price.cache_read_per_token,
                        "cache_creation_input_token_cost": price.cache_write_per_token,
                    }
                    for model, price in prices.items()
                }
            )
            if not normalized:
                raise ValueError("Price cache is empty")
            fetched_at = float(cache["fetched_at"])
            if not math.isfinite(fetched_at) or not 0 < fetched_at <= time.time() + 300:
                raise ValueError("Invalid price cache time")
            self.prices = PriceMap(normalized)
            self.fetched_at = fetched_at
            install_price_map(self.prices)
        except (OSError, ValueError, TypeError, KeyError, AttributeError, SQLAlchemyError):
            _logger.warning("Could not restore the usage price source cache")

    def status(self) -> dict[str, Any]:
        return {
            **self.selection.model_dump(),
            "url": redact_url(self.selection.url),
            "fetched_at": self.fetched_at,
            "last_attempt_at": self.last_attempt_at,
            "model_count": len(self.prices.prices) if self.prices else 0,
            "error": self.error,
        }

    def _save_cache(
        self, selection: PriceSourceSelection, prices: PriceMap, fetched_at: float
    ) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_suffix(".tmp")
        try:
            content = json.dumps(
                {
                    "selection": selection.model_dump(),
                    "fetched_at": fetched_at,
                    "prices": {model: asdict(price) for model, price in prices.prices.items()},
                },
                allow_nan=False,
            )
            if len(content.encode()) > _MAX_BYTES:
                raise ValueError("Price cache exceeds 10 MB")
            temporary.write_text(content)
            temporary.replace(self.cache_path)
        finally:
            temporary.unlink(missing_ok=True)

    async def select(self, selection: PriceSourceSelection) -> dict[str, Any]:
        async with self._lock:
            now = time.time()
            try:
                prices = await fetch_prices(selection) if selection.location() else None
            except ValueError as exc:
                await self._note_failure(str(exc), now)
                raise
            if prices is not None:
                await asyncio.to_thread(self._save_cache, selection, prices, now)
            await asyncio.to_thread(
                self.snapshots.write,
                RESERVED_USER_LOCAL,
                "usage.price_source",
                selection.model_dump(),
            )
            self.selection, self.prices = selection, prices
            self.fetched_at = now if prices is not None else None
            self.last_attempt_at, self.error = now, None
            install_price_map(prices)
            await self._fill()
            return self.status()

    async def _note_failure(self, message: str, at: float) -> None:
        self.error = message
        self.failures = [*self.failures[-15:], {"at": at, "error": message}]
        try:
            await asyncio.to_thread(
                self.snapshots.write,
                RESERVED_USER_LOCAL,
                "usage.price_source_failures",
                {"entries": self.failures},
            )
        except (ValueError, SQLAlchemyError):
            _logger.warning("Could not store price source failure")

    async def _fill(self) -> None:
        # Starts strictly before this cutoff have ended at least ten minutes ago.
        closed_before = int((time.time() - 600) // 3600) * 3600
        try:
            await asyncio.to_thread(
                self.hours.fill_unpriced, fetch_model_pricing_with_provider, closed_before
            )
        except Exception:
            _logger.warning("Could not fill unpriced usage hours", exc_info=True)

    async def refresh_if_due(self) -> None:
        async with self._lock:
            if self.selection.source == "builtin":
                return
            now = time.time()
            last = self.last_attempt_at or self.fetched_at
            if last is not None and now - last < _DAY:
                return
            self.last_attempt_at = now
            try:
                prices = await fetch_prices(self.selection)
                await asyncio.to_thread(self._save_cache, self.selection, prices, now)
            except (ValueError, OSError) as exc:
                message = (
                    str(exc) if isinstance(exc, ValueError) else "Price cache could not be saved"
                )
                await self._note_failure(message, now)
                return
            self.prices, self.fetched_at, self.error = prices, now, None
            install_price_map(prices)
            await self._fill()

    async def run(self) -> None:
        while True:
            await self.refresh_if_due()
            await asyncio.sleep(60)
