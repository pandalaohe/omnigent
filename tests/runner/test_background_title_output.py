"""Model-output validation at title dispatch and persistence boundaries."""

from unittest.mock import MagicMock

import pytest

from omnigent.runner.background_titles import service as title_service
from omnigent.runner.background_titles.service import BackgroundTitleContext
from omnigent.server.background_session_titles import (
    BACKGROUND_TITLE_MAX_CHARS,
    CUSTOM_BACKGROUND_TITLE_MAX_CHARS,
    BackgroundSessionTitleCoordinator,
    BackgroundTitleRequest,
    normalize_background_title,
)
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

_OUTPUT_CASES = [
    ("Debug authentication timeout", "Debug authentication timeout"),
    (
        "这个标题的任务是为用户意图生成简短标题。用户想检查重要任务。 **标题：工作目录重要任务**",
        "工作目录重要任务",
    ),
    (
        "The user wants help with a timeout.\n\n**Title:** Debug authentication timeout",
        "Debug authentication timeout",
    ),
    ("## **Title: `Debug authentication timeout`**", "Debug authentication timeout"),
    ("```text\nDebug authentication timeout\n```", "Debug authentication timeout"),
    ("- _Debug authentication timeout_", "Debug authentication timeout"),
    ("[Debug authentication timeout](https://example.test)", "Debug authentication timeout"),
    (
        '  "Debug   authentication timeout."\nAn explanation follows.',
        "Debug authentication timeout",
    ),
    ("**標題：工作目錄重要任務**", "工作目錄重要任務"),
    ("I need to generate a concise title.", None),
    ("The user is asking about important tasks.", None),
    ("这个标题的任务是为用户意图生成简短标题。", None),
    ("Here is a concise title:", None),
    ("Title: I should return only a short title.", None),
    ("Title: " + "x" * (BACKGROUND_TITLE_MAX_CHARS + 1), None),
    ("", None),
    ("x", None),
    ("Implement title generation", "Implement title generation"),
    (
        "I need a title: a short phrase.\nTitle: Debug authentication timeout",
        "Debug authentication timeout",
    ),
    ("Title: Fix title: metadata parsing", "Fix title: metadata parsing"),
    ("Fix `__init__.py` import", "Fix __init__.py import"),
    ("Analyze *args support", "Analyze *args support"),
    ("We need to generate a concise title.", None),
    ("I need help with pytest", "I need help with pytest"),
    ("Debug **authentication** timeout", "Debug authentication timeout"),
    ("**Title:**\nDebug authentication timeout", "Debug authentication timeout"),
    ("Title: Debug timeout\nTitle: Review authentication", None),
    ("Title: **Fix `__init__.py` import**", "Fix __init__.py import"),
    ("Inspect `__name__` handling", "Inspect __name__ handling"),
    ("Review `a*b*c` expression", "Review a*b*c expression"),
    ("Support *args and **kwargs", "Support *args and **kwargs"),
    ("`__name__`", "__name__"),
    ("__Title__: Debug timeout", "Debug timeout"),
]


@pytest.mark.parametrize(("raw", "expected"), _OUTPUT_CASES)
def test_normalize_title_output(raw: str, expected: str | None) -> None:
    assert normalize_background_title(raw) == expected
    assert normalize_background_title(expected) == expected


@pytest.mark.parametrize("harness", ["claude-native", "codex-native", "claude-sdk", "codex"])
@pytest.mark.parametrize(("raw", "expected"), _OUTPUT_CASES)
@pytest.mark.asyncio
async def test_all_generator_paths_validate_output(
    monkeypatch: pytest.MonkeyPatch, harness: str, raw: str, expected: str | None
) -> None:
    calls: list[BackgroundTitleContext] = []

    async def generate(context: BackgroundTitleContext) -> str:
        calls.append(context)
        return raw

    spec = title_service.generator_spec_for_harness(harness)
    assert spec is not None
    monkeypatch.setattr(spec.generator.replace(":", "."), generate)
    context = BackgroundTitleContext(
        prompt="Check important tasks",
        harness=harness,
        spawn_env={},
        process_manager=MagicMock(),
    )

    assert await title_service.generate_background_title(context) == expected
    # Invalid output keeps the seed instead of spending another model call on meta text.
    assert len(calls) == 1


@pytest.mark.parametrize("economy_fails", [False, True])
@pytest.mark.parametrize(
    "instructions",
    [
        "Use a detailed title.",
        "Use a detailed title.\n" + title_service.FOLLOW_USER_LANGUAGE_TITLE_INSTRUCTION,
    ],
)
@pytest.mark.asyncio
async def test_custom_title_limits_apply_after_model_fallback(
    monkeypatch: pytest.MonkeyPatch, economy_fails: bool, instructions: str
) -> None:
    async def generate(context: BackgroundTitleContext) -> str:
        if economy_fails and context.title_model:
            raise RuntimeError("Economy model unavailable")
        return "**Title: " + "x" * (CUSTOM_BACKGROUND_TITLE_MAX_CHARS + 1) + "**"

    monkeypatch.setattr(
        "omnigent.runner.background_titles.claude_native.generate_background_title", generate
    )
    context = BackgroundTitleContext(
        prompt="Review title limits",
        harness="claude-native",
        spawn_env={},
        process_manager=MagicMock(),
        additional_instructions=instructions,
    )

    assert await title_service.generate_background_title(context) == (
        "x" * (CUSTOM_BACKGROUND_TITLE_MAX_CHARS - 1) + "…"
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "The user wants help. **Title: Debug authentication timeout**",
            "Debug authentication timeout",
        ),
        ("I need to generate a concise title.", "Check important tasks"),
        ("Title: " + "x" * (BACKGROUND_TITLE_MAX_CHARS + 1), "Check important tasks"),
        ("We need to generate a concise title.", "Check important tasks"),
        ("Title: Fix title: metadata parsing", "Fix title: metadata parsing"),
        ("Fix `__init__.py` import", "Fix __init__.py import"),
        ("Analyze *args support", "Analyze *args support"),
        ("Inspect `__name__` handling", "Inspect __name__ handling"),
        ("Review `a*b*c` expression", "Review a*b*c expression"),
        ("Support *args and **kwargs", "Support *args and **kwargs"),
        (
            "I need a title: a short phrase.\nTitle: Debug authentication timeout",
            "Debug authentication timeout",
        ),
    ],
)
@pytest.mark.asyncio
async def test_invalid_model_output_keeps_seed_title(db_uri: str, raw: str, expected: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conversation = store.create_conversation(kind="default", title="Check important tasks")

    async def generate(_request: BackgroundTitleRequest) -> str:
        # Exercise the runner-to-server boundary, where normalization happens twice.
        title = normalize_background_title(raw)
        return title if title is not None else raw

    coordinator = BackgroundSessionTitleCoordinator(store, generate)
    coordinator.schedule(
        session_id=conversation.id,
        prompt="Check important tasks",
        expected_seed_title="Check important tasks",
    )
    await coordinator.wait_for_idle()

    stored = store.get_conversation(conversation.id)
    assert stored is not None
    assert stored.title == expected
