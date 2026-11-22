"""Reasoning-effort validation helpers shared across client/runtime paths."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from types import MappingProxyType

from omnigent.llms.errors import PermanentLLMError

EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
EFFORT_VALUES = frozenset(EFFORT_ORDER)
EFFORT_CLEAR_VALUES = frozenset({"default", "off", "reset"})

# Fold a value to a canonical one, but only where the target ladder lacks it:
# ``validate_effort`` applies an alias only when the raw value is unsupported
# but the canonical one is. When normalizing the copied ChatGPT-app config
# default against ``CODEX_EFFORTS`` (capped at ``xhigh``), ``ultra`` / retired
# ``max`` fold to ``xhigh``; explicit codex turns use ``CODEX_NATIVE_EFFORTS``
# and keep those values unchanged.
EFFORT_ALIASES: dict[str, str] = {"ultra": "xhigh", "max": "xhigh"}

OPENAI_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh"})
ANTHROPIC_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
CLAUDE_EFFORTS = ANTHROPIC_EFFORTS
CODEX_EFFORTS = OPENAI_EFFORTS
# Codex advertises per-model reasoning levels via ``model/list``. Native launch
# and dispatch clamp incompatible pairs; accept the full vocabulary here so
# supported ``max``/``ultra`` settings survive harness-level validation.
CODEX_NATIVE_EFFORTS = EFFORT_VALUES
OPENAI_AGENTS_EFFORTS = OPENAI_EFFORTS
GEMINI_EFFORTS = frozenset({"low", "medium", "high"})
# agy supports xhigh on Gemini 4 Neon / Hydrogen; models that top out lower
# (e.g. Opus/Sonnet 5.5) are capped per model via ANTIGRAVITY_MODEL_EFFORT_CAPS.
ANTIGRAVITY_EFFORTS = frozenset({"low", "medium", "high", "xhigh"})
# The GitHub Copilot SDK's ``create_session(reasoning_effort=...)`` accepts
# exactly these levels (``copilot.session.ReasoningEffort`` literal); per-model
# support is gated by the Copilot backend (``list_models()``).
COPILOT_EFFORTS = frozenset({"low", "medium", "high", "xhigh"})
# pi's ``--thinking`` ladder is ``off|minimal|low|medium|high|xhigh|max``. Its
# ``off`` is omnigent's ``none`` — ``off`` is reserved here as a
# clear-to-default sentinel — so :func:`to_pi_thinking_level` translates.
PI_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"})

#: pi's spelling of "no thinking", i.e. canonical ``none``.
PI_THINKING_OFF = "off"


def to_pi_thinking_level(effort: str) -> str:
    """Translate a canonical effort to pi's ``--thinking`` / RPC level.

    ``none`` maps to pi's ``off``; ``ultra`` (not in pi's ladder) clamps to
    ``max`` (the highest rung pi advertises); every other value is identical.

    :param effort: A canonical effort from :data:`PI_EFFORTS`.
    :returns: The ``ThinkingLevel`` string pi accepts.
    """
    if effort == "none":
        return PI_THINKING_OFF
    # PI_THINKING_LADDER is resolved at call time (defined later in this module).
    if effort not in PI_THINKING_LADDER:
        return PI_THINKING_LADDER[-1]  # clamp ultra → max (pi's highest rung)
    return effort


#: pi's ladder ascending, in pi's spelling. Index distance here is what
#: "nearest supported level" means.
PI_THINKING_LADDER: tuple[str, ...] = (
    PI_THINKING_OFF,
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)


def nearest_pi_thinking_level(level: str, available: Iterable[str]) -> str | None:
    """Clamp a pi thinking *level* to the nearest one the model supports.

    pi reports a per-model ladder via the ``get_available_thinking_levels``
    RPC; a model that doesn't offer the requested rung must not 400 the turn,
    so pick the closest offered rung instead. Ties resolve downward, spending
    fewer tokens than asked for rather than more.

    :param level: The requested level in pi's spelling (see
        :func:`to_pi_thinking_level`).
    :param available: Levels pi reports for the current model.
    :returns: The level to send, or ``None`` when *available* is empty (pi
        reported nothing usable, so the caller should leave the level alone).
    """
    offered = [value for value in PI_THINKING_LADDER if value in set(available)]
    if not offered:
        return None
    if level in offered:
        return level
    if level not in PI_THINKING_LADDER:
        return None
    target = PI_THINKING_LADDER.index(level)
    # Ascending order + stable min(): an equidistant pair resolves downward.
    return min(offered, key=lambda value: abs(PI_THINKING_LADDER.index(value) - target))


def efforts_for_harness(harness: str | None) -> frozenset[str] | None:
    """
    Return the effort vocabulary *harness* accepts.

    Three outcomes, and the difference between the last two matters:

    - a non-empty set — the harness takes an effort from that vocabulary;
    - an empty set — the harness is known and has no effort plumbing
      (``EffortFamily.NONE``), so any effort is meaningless to it;
    - ``None`` — the harness is not in the capability registry, so it
      cannot be classified. Callers that filter should pass the value
      through rather than drop it, since a plugin-registered harness may
      handle an effort this process knows nothing about.

    :param harness: Harness name or alias, e.g. ``"claude-native"``.
    :returns: Accepted values, an empty set, or ``None`` when unknown.
    """
    # Imported inside the function: harness_plugins pulls in a large slice
    # of the package, and a module-level edge from here has produced an
    # import cycle before.
    from omnigent.harness_aliases import canonicalize_harness
    from omnigent.harness_capabilities import EffortFamily
    from omnigent.harness_plugins import harness_capabilities

    if harness is None:
        return None
    canonical = canonicalize_harness(harness) or harness
    capabilities = harness_capabilities().get(canonical)
    if capabilities is None:
        return None
    if capabilities.effort is EffortFamily.DYNAMIC:
        return None
    # Every family that has a vocabulary must be listed: an unmapped family
    # falls through to the empty set, which callers read as "known harness with
    # no effort plumbing" and silently drop the effort. A new EffortFamily
    # therefore has to be added here in the same change that declares it.
    return {
        EffortFamily.ANTHROPIC: ANTHROPIC_EFFORTS,
        EffortFamily.OPENAI: OPENAI_EFFORTS,
        EffortFamily.GEMINI: GEMINI_EFFORTS,
        EffortFamily.ANTIGRAVITY: ANTIGRAVITY_EFFORTS,
        EffortFamily.COPILOT: COPILOT_EFFORTS,
        EffortFamily.PI: PI_EFFORTS,
        EffortFamily.CODEX_NATIVE: CODEX_NATIVE_EFFORTS,
    }.get(capabilities.effort, frozenset())


def format_supported(values: Iterable[str]) -> str:
    """Return a stable comma-separated supported-values string."""
    order = ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
    values_set = set(values)
    return ", ".join(value for value in order if value in values_set)


def unsupported_effort_message(effort: str, provider: str, supported: Iterable[str]) -> str:
    """Build a clear unsupported-effort error message."""
    return (
        f"Effort {effort!r} is not supported by {provider}; "
        f"supported values: {format_supported(supported)}"
    )


# Some models served through a harness reject the harness's full effort ladder.
# GLM on the codex/Responses wire accepts only up to ``high`` (no ``xhigh``), so
# a user default of ``xhigh``/``max`` 400s the turn. Map such a model to the
# effort to use instead of failing — GLM falls back to ``medium``.
#
# These are probed facts about ONE gateway's serving, not properties of the
# effort ladders themselves, so a deployment whose gateway caps other models
# overrides them via ``routing.effort_caps`` (see
# :class:`~omnigent.server.smart_routing.RoutingSettings`). The provider ladders
# above stay frozen: they are the wire APIs' own vocabularies.
_MODEL_EFFORT_FALLBACK: Mapping[str, str] = MappingProxyType({"glm-5-2": "medium"})
# Efforts a fallback model cannot accept, so a pinned high value coerces down.
# GLM tops out at ``high``, so every rung above it (``xhigh``/``max``/``ultra``)
# is unsupported.
_MODEL_EFFORT_UNSUPPORTED: Mapping[str, frozenset[str]] = MappingProxyType(
    {"glm-5-2": frozenset({"xhigh", "max", "ultra"})}
)


@dataclass(frozen=True)
class ModelEffortCaps:
    """Per-model effort ceilings a deployment's gateway imposes.

    :param fallback: Bare model id → the effort to use when the requested one
        is barred. Also the effort a switch onto that model sends when the
        caller asked for none.
    :param unsupported: Bare model id → the efforts that model's backend
        rejects outright.
    """

    # default_factory, not default: a mapping is unhashable and dataclasses
    # rejects an unhashable default outright.
    fallback: Mapping[str, str] = field(default_factory=lambda: _MODEL_EFFORT_FALLBACK)
    unsupported: Mapping[str, frozenset[str]] = field(
        default_factory=lambda: _MODEL_EFFORT_UNSUPPORTED
    )


#: The caps every deployment gets unless its ``routing:`` block overrides them.
DEFAULT_MODEL_EFFORT_CAPS = ModelEffortCaps()

# agy serves several models that top out below ``xhigh`` (``agy models`` offers
# ``xhigh`` only for Gemini 4 Neon / Hydrogen). Kept separate from
# ``DEFAULT_MODEL_EFFORT_CAPS`` because Opus/Sonnet 5.5 support ``xhigh``/``max``
# on Anthropic harnesses and because ``routing.effort_caps`` overrides the
# gateway table independently of agy's CLI catalog.
_ANTIGRAVITY_ABOVE_HIGH: frozenset[str] = frozenset({"xhigh", "max", "ultra"})
_ANTIGRAVITY_NON_MEDIUM: frozenset[str] = frozenset({"low", "high", "xhigh", "max", "ultra"})
_ANTIGRAVITY_LADDER: tuple[str, ...] = ("low", "medium", "high", "xhigh")
_ANTIGRAVITY_DISPLAY_EFFORT_RE = re.compile(
    r"\s*\((?:low|medium|high|xhigh|max)\)$", re.IGNORECASE
)
_ANTIGRAVITY_SLUG_EFFORT_RE = re.compile(r"-(?:low|medium|high|xhigh|max)$", re.IGNORECASE)


def _strip_antigravity_effort_suffix(model: str) -> str:
    """Strip a trailing agy display-name or slug effort suffix."""
    without_display = _ANTIGRAVITY_DISPLAY_EFFORT_RE.sub("", model.strip())
    return _ANTIGRAVITY_SLUG_EFFORT_RE.sub("", without_display).strip()


def _antigravity_model_identity(model: str) -> str:
    """Fold an agy model id or display name into a comparison key."""
    base = _strip_antigravity_effort_suffix(model)
    return re.sub(r"[^a-z0-9]", "", _bare_model(base))


def _antigravity_row_efforts(row: Mapping[str, object]) -> frozenset[str] | None:
    """Extract advertised effort values from one agy catalog row."""
    raw = row.get("efforts")
    if isinstance(raw, (list, tuple)):
        levels = frozenset(v for v in raw if isinstance(v, str) and v)
        if levels:
            return levels
    supported = row.get("supportedReasoningEfforts")
    if isinstance(supported, (list, tuple)):
        levels = frozenset(
            item["reasoningEffort"]
            for item in supported
            if isinstance(item, Mapping)
            and isinstance(item.get("reasoningEffort"), str)
            and item["reasoningEffort"]
        )
        if levels:
            return levels
    return None


@lru_cache(maxsize=1)
def antigravity_model_effort_caps() -> ModelEffortCaps:
    """Return per-model effort ceilings for Antigravity (``agy``) models.

    Imported lazily because :mod:`omnigent.models.model_fallbacks` pulls in
    ``provider_config`` → ``spec.parser`` → ``spec.validator`` → this module.
    """
    from omnigent.models.model_fallbacks import ANTIGRAVITY_MEDIUM_CAP_MODELS

    return ModelEffortCaps(
        fallback=MappingProxyType(
            dict.fromkeys(ANTIGRAVITY_MEDIUM_CAP_MODELS.model_ids, "medium")
        ),
        unsupported=MappingProxyType(
            dict.fromkeys(ANTIGRAVITY_MEDIUM_CAP_MODELS.model_ids, _ANTIGRAVITY_NON_MEDIUM)
        ),
    )


@lru_cache(maxsize=1)
def _antigravity_xhigh_identities() -> frozenset[str]:
    """Return normalized identities of agy models verified to offer ``xhigh``."""
    from omnigent.models.model_fallbacks import ANTIGRAVITY_XHIGH_MODELS

    return frozenset(_antigravity_model_identity(m) for m in ANTIGRAVITY_XHIGH_MODELS.model_ids)


def clamp_antigravity_effort(
    effort: str | None,
    model: str | None,
    *,
    catalog_models: Iterable[Mapping[str, object]] | None = None,
) -> str | None:
    """Clamp an Antigravity *effort* fail-closed against *model* and *catalog_models*.

    ``xhigh`` (and higher aliases ``max``/``ultra``) is allowed only when the
    resolved model is known to offer ``xhigh`` — either because its row in
    *catalog_models* advertises ``xhigh``, or (when no catalog row matches) its
    normalized identity is in :data:`ANTIGRAVITY_XHIGH_MODELS`. Any other or
    unknown model, including ``model=None``, fails closed to ``"high"`` (or the
    highest/nearest rung the model supports, such as ``"medium"`` for
    medium-only models).
    """
    if effort is None:
        return None
    identity = (
        _antigravity_model_identity(model) if isinstance(model, str) and model.strip() else ""
    )
    matched_row: Mapping[str, object] | None = None
    if identity and catalog_models is not None:
        for row in catalog_models:
            if not isinstance(row, Mapping):
                continue
            row_id = row.get("id")
            row_display = row.get("displayName")
            if (isinstance(row_id, str) and _antigravity_model_identity(row_id) == identity) or (
                isinstance(row_display, str)
                and _antigravity_model_identity(row_display) == identity
            ):
                matched_row = row
                break
    resolved_model: str | None = None
    if matched_row is not None and isinstance(matched_row.get("id"), str) and matched_row["id"]:
        resolved_model = str(matched_row["id"])
    elif isinstance(model, str) and model.strip():
        resolved_model = re.sub(r"[\s.]+", "-", _strip_antigravity_effort_suffix(model).lower())

    clamped = clamp_effort_for_model(effort, resolved_model, caps=antigravity_model_effort_caps())
    assert clamped is not None

    row_efforts = _antigravity_row_efforts(matched_row) if matched_row is not None else None
    if row_efforts is not None:
        offered = [level for level in _ANTIGRAVITY_LADDER if level in row_efforts]
        if offered:
            if clamped in _ANTIGRAVITY_ABOVE_HIGH:
                return "xhigh" if "xhigh" in offered else offered[-1]
            if clamped in offered:
                return clamped
            if clamped in _ANTIGRAVITY_LADDER:
                target = _ANTIGRAVITY_LADDER.index(clamped)
                return min(
                    offered, key=lambda value: abs(_ANTIGRAVITY_LADDER.index(value) - target)
                )
        return clamped

    if clamped in _ANTIGRAVITY_ABOVE_HIGH:
        if identity and identity in _antigravity_xhigh_identities():
            return "xhigh"
        return "high"
    return clamped


def model_effort_caps(caps: ModelEffortCaps | None = None) -> ModelEffortCaps:
    """Resolve which effort caps apply, defaulting to this deployment's.

    ``None`` reads :class:`~omnigent.server.smart_routing.RoutingSettings` off
    the process caps, so a managed gateway that caps a different model set is
    honoured without every caller threading the value. Outside a server process
    (the runner holds no routing settings) that read yields the defaults, which
    are the frozen tables above — so runner-side clamping is unchanged.

    :param caps: Explicit caps, or ``None`` to read the deployment's.
    :returns: The caps to clamp with; never ``None``.
    """
    if caps is not None:
        return caps
    try:
        from omnigent.server.smart_routing import routing_settings
    except ImportError:  # pragma: no cover — a build without the server extra
        return DEFAULT_MODEL_EFFORT_CAPS
    return routing_settings().model_effort_caps or DEFAULT_MODEL_EFFORT_CAPS


def _bare_model(model: str) -> str:
    """Strip a catalog/gateway prefix and fold to the comparison spelling."""
    bare = model.rsplit("/", 1)[-1]
    for prefix in ("databricks-", "system.ai."):
        if bare.startswith(prefix):
            bare = bare[len(prefix) :]
    return bare.replace(".", "-").lower()


def clamp_effort_for_model(
    effort: str | None,
    model: str | None,
    *,
    caps: ModelEffortCaps | None = None,
) -> str | None:
    """Coerce *effort* to one *model* accepts, keeping the user's pick otherwise.

    A model whose backend rejects a high effort (e.g. GLM has no ``xhigh``)
    falls back to a supported value rather than 400-ing the turn. Any other
    model, or an already-accepted effort, is returned unchanged.

    :param caps: Effort ceilings to clamp against; ``None`` uses this
        deployment's (see :func:`model_effort_caps`).
    """
    if effort is None or model is None:
        return effort
    resolved = model_effort_caps(caps)
    key = _bare_model(model)
    unsupported = resolved.unsupported.get(key)
    if unsupported is not None and effort in unsupported:
        return resolved.fallback.get(key, effort)
    return effort


def effort_for_model_switch(
    effort: str | None,
    model: str | None,
    *,
    caps: ModelEffortCaps | None = None,
) -> str | None:
    """Effort to send when switching to *model*, guarding a rejected default.

    Like :func:`clamp_effort_for_model` for an explicit *effort*. When *effort*
    is ``None`` (no effort requested), a model that caps its ladder still needs
    guarding, because the switched-to thread inherits the config's default
    (which may be too high): return that model's fallback so the live turn does
    not 400. A model with no cap and no requested effort returns ``None``.

    :param caps: Effort ceilings to clamp against; ``None`` uses this
        deployment's (see :func:`model_effort_caps`).
    """
    if effort is not None:
        return clamp_effort_for_model(effort, model, caps=caps)
    if model is None:
        return None
    return model_effort_caps(caps).fallback.get(_bare_model(model))


def validate_effort(effort: object, provider: str, supported: Iterable[str] | None) -> str | None:
    """Validate *effort* against *supported*, returning a string or None.

    A deprecated alias (see :data:`EFFORT_ALIASES`) is coerced to its
    canonical value when the raw value is unsupported but the canonical one
    is — e.g. the ChatGPT app's ``ultra`` becomes ``xhigh`` for codex, while
    ``max`` stays ``max`` for providers that still support it (Anthropic).
    """
    if effort is None or effort == "":
        return None
    if supported is None:
        if (
            not isinstance(effort, str)
            or not effort.strip()
            or len(effort) > 128
            or any(ord(char) < 32 for char in effort)
        ):
            raise ValueError(
                "effort must be a non-empty string of at most 128 characters "
                "without control characters"
            )
        return effort
    effort_str = str(effort)
    supported_set = set(supported)
    if effort_str not in supported_set:
        alias = EFFORT_ALIASES.get(effort_str)
        if alias is not None and alias in supported_set:
            return alias
        raise ValueError(unsupported_effort_message(effort_str, provider, supported_set))
    return effort_str


def validate_effort_for_harness(effort: str, harness: str | None) -> str:
    """
    Validate an explicitly requested *effort* against the harness's declared family.

    A harness whose family is ``NONE`` has no effort plumbing, and an
    unrecognized one cannot be checked at all — the caller asked for this
    explicitly, so reject rather than accept-and-drop. (Delivery of a
    persisted effort takes the opposite tack and filters quietly; see the
    reasoning guard in :mod:`omnigent.runner.app`.)

    :param effort: Requested effort, e.g. ``"high"``.
    :param harness: Resolved harness, e.g. ``"claude-native"``.
    :returns: The canonical effort that harness accepts.
    :raises ValueError: If the harness supports no effort override, or
        the value falls outside its vocabulary.
    """
    from omnigent.harness_aliases import canonicalize_harness

    canonical = canonicalize_harness(harness) if harness is not None else None
    supported = efforts_for_harness(harness)
    if not supported:
        raise ValueError(
            f"harness {canonical or harness or 'unknown'!r} does not support "
            "a reasoning-effort override"
        )
    validated = validate_effort(effort, canonical or harness or "unknown", supported)
    assert validated is not None
    return validated


def validate_effort_or_llm_error(
    effort: object,
    provider: str,
    supported: Iterable[str],
) -> str | None:
    """Validate for native LLM paths, raising non-retryable PermanentLLMError."""
    try:
        return validate_effort(effort, provider, supported)
    except ValueError as exc:
        raise PermanentLLMError(str(exc), code="unsupported_reasoning_effort") from exc
