"""Resolve saved model ids against a harness catalog without rewriting settings."""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

Outcome = Literal["unverified", "exact", "renamed", "pinned", "substituted", "missing"]
ProviderGroups = Mapping[str, frozenset[str]]
ALIASES_NAMESPACE = "model_provider_aliases"


def alias_pairs(value: object) -> tuple[tuple[str, str], ...]:
    """Validate the owner-editable rename table before it is persisted."""
    if not isinstance(value, dict) or set(value) != {"pairs"}:
        raise ValueError("Provider aliases must contain a pairs array")
    pairs = value["pairs"]
    if not isinstance(pairs, list) or len(pairs) > 64:
        raise ValueError("Provider aliases must contain at most 64 pairs")
    result = []
    for pair in pairs:
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or any(not isinstance(name, str) or not name.strip() or "/" in name for name in pair)
        ):
            raise ValueError(
                "Each alias pair must contain two non-empty provider names without slashes"
            )
        result.append((pair[0], pair[1]))
    return tuple(result)


@dataclass(frozen=True)
class ModelResolution:
    saved: str
    model: str
    row_id: str | None
    outcome: Outcome
    candidates: tuple[str, ...] = ()


def split_model_id(model: str) -> tuple[str | None, str]:
    """Only the first slash separates the provider from the model."""
    provider, separator, name = model.partition("/")
    return (provider, name) if separator else (None, model)


def provider_groups(pairs: Iterable[tuple[str, str]]) -> ProviderGroups:
    """Build transitive, bidirectional rename groups from provider-name pairs."""
    groups: dict[str, frozenset[str]] = {}
    for left, right in pairs:
        if any(not name.strip() or "/" in name for name in (left, right)):
            raise ValueError("Provider names must be non-empty and contain no slash")
        group = groups.get(left, frozenset({left})) | groups.get(right, frozenset({right}))
        for name in group:
            groups[name] = group
    return groups


def equivalent_ids(model: str, groups: ProviderGroups) -> tuple[str, ...]:
    provider, name = split_model_id(model)
    if provider is None or provider not in groups:
        return (model,)
    return (model, *(f"{other}/{name}" for other in sorted(groups[provider] - {provider})))


def canonical_model(model: str, groups: ProviderGroups) -> str:
    """Give every id in one provider group the same quota/limit identity."""
    provider, name = split_model_id(model)
    if provider is None or provider not in groups:
        return model
    return f"{min(groups[provider])}/{name}"


def canonical_pool(pool: str, groups: ProviderGroups) -> str:
    product, separator, model = pool.partition(":")
    return f"{product}:{canonical_model(model, groups)}" if separator else pool


def canonical_pool_values[T](
    values: Mapping[str, T], groups: ProviderGroups, *, preferred: str | None = None
) -> dict[str, T]:
    """Read one value per pool: the saved key wins, else the first raw key."""
    result = {}
    for key in sorted(values):
        result.setdefault(canonical_pool(key, groups), values[key])
    if preferred is not None and preferred in values:
        result[canonical_pool(preferred, groups)] = values[preferred]
    return result


def resolve_saved_model(
    model: str,
    catalog: Collection[str] | None,
    groups: ProviderGroups,
    *,
    accepts: Callable[[str], str | None] | None = None,
) -> ModelResolution:
    """Prefer listed equivalents and route pins before a unique suffix match.

    A missing result describes catalog evidence; the consumer decides whether
    to attempt the launch or use its next configured option.
    """
    if not catalog:
        return ModelResolution(model, model, None, "unverified")
    if model in catalog:
        return ModelResolution(model, model, model, "exact")
    equivalents = equivalent_ids(model, groups)
    for equivalent in equivalents[1:]:
        if equivalent in catalog:
            return ModelResolution(model, equivalent, equivalent, "renamed")
    if accepts is not None:
        for equivalent in equivalents:
            row_id = accepts(equivalent)
            if row_id is not None and row_id in catalog:
                return ModelResolution(model, equivalent, row_id, "pinned")
    name = split_model_id(model)[1]
    candidates = tuple(sorted({value for value in catalog if split_model_id(value)[1] == name}))
    if len(candidates) == 1:
        return ModelResolution(model, candidates[0], candidates[0], "substituted")
    return ModelResolution(model, model, None, "missing", candidates)
