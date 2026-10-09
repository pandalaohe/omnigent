"""Tests for collaboration config and the repository/binding stores.

Covers repository upsert (revision bump on change only, role demotion),
binding writes (``is_primary`` derived from the project's code repository,
never client-set), and the per-host project entries (upsert / list / delete /
existence guard).
"""

from __future__ import annotations

import uuid

import pytest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.stores.project_host_binding_store.sqlalchemy_store import (
    SqlAlchemyProjectHostBindingStore,
)
from omnigent.stores.project_repository_store.sqlalchemy_store import (
    SqlAlchemyProjectRepositoryStore,
)
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore


def _uid(seed: str) -> str:
    """Deterministic bare 32-char hex UUID string from a short readable seed."""
    return uuid.uuid5(uuid.NAMESPACE_DNS, seed).hex


@pytest.fixture()
def project_store(db_uri: str) -> SqlAlchemyProjectStore:
    """A fresh :class:`SqlAlchemyProjectStore` backed by the test SQLite DB."""
    return SqlAlchemyProjectStore(db_uri)


@pytest.fixture()
def repository_store(db_uri: str) -> SqlAlchemyProjectRepositoryStore:
    """A fresh repository store backed by the test SQLite DB."""
    return SqlAlchemyProjectRepositoryStore(db_uri)


@pytest.fixture()
def binding_store(db_uri: str) -> SqlAlchemyProjectHostBindingStore:
    """A fresh binding store backed by the test SQLite DB."""
    return SqlAlchemyProjectHostBindingStore(db_uri)


def _create_project(
    project_store: SqlAlchemyProjectStore, seed: str = "proj", name: str = "P"
) -> str:
    """Create a project and return its id (bindings/repositories lock it)."""
    project_id = _uid(seed)
    project_store.create(project_id, name, "alice@example.com")
    return project_id


def _create_repo(
    repository_store: SqlAlchemyProjectRepositoryStore,
    project_id: str,
    name: str = "root",
    *,
    role: str | None = None,
) -> str:
    """Register a repository and return its id (bindings must name a real one)."""
    repository, _changed = repository_store.apply_repository(
        project_id=project_id,
        name=name,
        remote_url="git@github.com:example/repo.git",
        default_branch="main",
        role=role,
    )
    return repository.id


# ── repositories ────────────────────────────────────────────────────────


def test_repository_upsert_inserts_at_revision_1(
    repository_store: SqlAlchemyProjectRepositoryStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """A new registration starts at revision 1 and role related."""
    _create_project(project_store)
    repo, changed = repository_store.apply_repository(
        project_id=_uid("proj"),
        name="root",
        remote_url="git@github.com:example/repo.git",
        default_branch="main",
    )
    assert repo.revision == 1
    assert repo.role == "related"
    assert repo.context_manifest_path == ".agents/project/manifest.json"
    assert repo.created_at > 0
    assert repo.updated_at is None
    assert changed == []


def test_repository_upsert_identical_is_noop(
    repository_store: SqlAlchemyProjectRepositoryStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """Re-registering unchanged values returns the row without a bump."""
    _create_project(project_store)
    first, _ = repository_store.apply_repository(
        project_id=_uid("proj"),
        name="root",
        remote_url="git@github.com:example/repo.git",
        default_branch="main",
    )
    second, _ = repository_store.apply_repository(
        project_id=_uid("proj"),
        name="root",
        remote_url="git@github.com:example/repo.git",
        default_branch="main",
    )
    assert second.id == first.id
    assert second.revision == 1
    assert second.updated_at is None


def test_repository_upsert_change_bumps_revision(
    repository_store: SqlAlchemyProjectRepositoryStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """A changed field bumps the revision and stamps ``updated_at``."""
    _create_project(project_store)
    repository_store.apply_repository(
        project_id=_uid("proj"),
        name="root",
        remote_url="git@github.com:example/repo.git",
        default_branch="main",
    )
    updated, _ = repository_store.apply_repository(
        project_id=_uid("proj"),
        name="root",
        remote_url="git@github.com:example/other.git",
        default_branch="main",
    )
    assert updated.revision == 2
    assert updated.remote_url == "git@github.com:example/other.git"
    assert updated.updated_at is not None


def test_repository_get_list_delete(
    repository_store: SqlAlchemyProjectRepositoryStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """``get`` / ``get_by_name`` / ``list_by_project`` / ``delete`` round-trip."""
    _create_project(project_store)
    repo, _ = repository_store.apply_repository(
        project_id=_uid("proj"),
        name="root",
        remote_url="u",
        default_branch="main",
    )
    assert repository_store.get(repo.id) == repo
    assert repository_store.get_by_name(project_id=_uid("proj"), name="root") == repo
    assert repository_store.get(_uid("nope")) is None
    assert repository_store.get_by_name(project_id=_uid("proj"), name="nope") is None
    assert [r.name for r in repository_store.list_by_project(_uid("proj"))] == ["root"]
    assert repository_store.list_by_project(_uid("other")) == []
    assert repository_store.delete(repo.id) is True
    assert repository_store.delete(repo.id) is False
    assert repository_store.get(repo.id) is None


def test_repository_role_empty_remote_accepted(
    repository_store: SqlAlchemyProjectRepositoryStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """A repository with no git location stores an empty remote."""
    _create_project(project_store)
    repo, _ = repository_store.apply_repository(
        project_id=_uid("proj"),
        name="root",
        remote_url="",
        default_branch="main",
    )
    assert repo.remote_url == ""


def test_repository_apply_unknown_role_rejected(
    repository_store: SqlAlchemyProjectRepositoryStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """A role outside code/related is refused and writes nothing."""
    project_id = _create_project(project_store)
    with pytest.raises(OmnigentError) as exc:
        repository_store.apply_repository(
            project_id=project_id,
            name="root",
            remote_url="u",
            default_branch="main",
            role="primary",
        )
    assert exc.value.code == ErrorCode.INVALID_INPUT
    assert repository_store.list_by_project(project_id) == []


def test_marking_code_demotes_the_other_repository(
    repository_store: SqlAlchemyProjectRepositoryStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """Exactly one code repository: the newly marked one wins."""
    project_id = _create_project(project_store)
    first = _create_repo(repository_store, project_id, "a", role="code")
    second = _create_repo(repository_store, project_id, "b")
    assert repository_store.get(first).role == "code"

    marked, _ = repository_store.apply_repository(
        project_id=project_id,
        name="b",
        remote_url="git@github.com:example/repo.git",
        default_branch="main",
        role="code",
    )
    assert marked.role == "code"
    demoted = repository_store.get(first)
    assert demoted is not None
    assert demoted.role == "related"
    assert demoted.revision == 2
    assert demoted.updated_at is not None
    assert repository_store.get(second).role == "code"


def test_repeated_role_writes_end_consistent(
    repository_store: SqlAlchemyProjectRepositoryStore,
    binding_store: SqlAlchemyProjectHostBindingStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """Serialized role flips leave one code repository and its host primaries."""
    project_id = _create_project(project_store)
    host_id = _uid("host-a")
    a = _create_repo(repository_store, project_id, "a")
    b = _create_repo(repository_store, project_id, "b")
    bindings = {
        repo_id: binding_store.apply_binding(
            project_id=project_id,
            host_id=host_id,
            name=repo_id,
            repository_id=repo_id,
            workspace=f"/opt/work/{repo_id}",
        )
        for repo_id in (a, b)
    }

    for name in ("a", "b", "a"):
        repository_store.apply_repository(
            project_id=project_id,
            name=name,
            remote_url="git@github.com:example/repo.git",
            default_branch="main",
            role="code",
        )

    roles = {repo.name: repo.role for repo in repository_store.list_by_project(project_id)}
    assert roles == {"a": "code", "b": "related"}
    assert binding_store.get(bindings[a].id).is_primary is True
    assert binding_store.get(bindings[b].id).is_primary is False


def test_repository_apply_derives_primary_bindings(
    repository_store: SqlAlchemyProjectRepositoryStore,
    binding_store: SqlAlchemyProjectHostBindingStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """Marking code promotes that repository's enabled binding."""
    project_id = _create_project(project_store)
    repo_id = _create_repo(repository_store, project_id)
    binding = binding_store.apply_binding(
        project_id=project_id,
        host_id=_uid("host-a"),
        name="primary",
        repository_id=repo_id,
        workspace="/w",
    )
    assert binding.is_primary is False

    marked, changed = repository_store.apply_repository(
        project_id=project_id,
        name="root",
        remote_url="git@github.com:example/repo.git",
        default_branch="main",
        role="code",
    )
    assert marked.role == "code"
    assert [row.id for row in changed] == [binding.id]
    promoted = binding_store.get(binding.id)
    assert promoted is not None
    assert promoted.is_primary is True
    assert promoted.revision == 2
    assert changed[0].revision == 2


# ── bindings ────────────────────────────────────────────────────────────


def test_binding_apply_inserts_at_revision_1_never_primary(
    binding_store: SqlAlchemyProjectHostBindingStore,
    repository_store: SqlAlchemyProjectRepositoryStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """Without a code repository no binding is primary."""
    project_id = _create_project(project_store)
    repo_id = _create_repo(repository_store, project_id)
    binding = binding_store.apply_binding(
        project_id=project_id,
        host_id=_uid("host-a"),
        name="primary",
        repository_id=repo_id,
        workspace="/opt/work/p",
    )
    assert binding.revision == 1
    assert binding.is_primary is False
    assert binding.enabled is True
    assert binding.path_verified_at is not None


def test_binding_apply_unverified_keeps_null_timestamp(
    binding_store: SqlAlchemyProjectHostBindingStore,
    repository_store: SqlAlchemyProjectRepositoryStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """An offline save stores the path with no verification stamp."""
    project_id = _create_project(project_store)
    repo_id = _create_repo(repository_store, project_id)
    binding = binding_store.apply_binding(
        project_id=project_id,
        host_id=_uid("host-a"),
        name="primary",
        repository_id=repo_id,
        workspace="/opt/work/typed/",
        verified=False,
    )
    assert binding.workspace == "/opt/work/typed/"
    assert binding.path_verified_at is None


def test_binding_apply_unverified_clears_a_stale_verification(
    binding_store: SqlAlchemyProjectHostBindingStore,
    repository_store: SqlAlchemyProjectRepositoryStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """An offline edit of a verified binding drops the old timestamp."""
    project_id = _create_project(project_store)
    repo_id = _create_repo(repository_store, project_id)
    verified = binding_store.apply_binding(
        project_id=project_id,
        host_id=_uid("host-a"),
        name="primary",
        repository_id=repo_id,
        workspace="/opt/work/a",
    )
    assert verified.path_verified_at is not None
    offline = binding_store.apply_binding(
        project_id=project_id,
        host_id=_uid("host-a"),
        name="primary",
        repository_id=repo_id,
        workspace="/opt/work/b",
        verified=False,
    )
    assert offline.workspace == "/opt/work/b"
    assert offline.path_verified_at is None
    assert binding_store.get(verified.id).path_verified_at is None


def test_binding_apply_change_bumps_revision(
    binding_store: SqlAlchemyProjectHostBindingStore,
    repository_store: SqlAlchemyProjectRepositoryStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """A changed path bumps the revision."""
    project_id = _create_project(project_store)
    repo_id = _create_repo(repository_store, project_id)
    binding_store.apply_binding(
        project_id=project_id,
        host_id=_uid("host-a"),
        name="primary",
        repository_id=repo_id,
        workspace="/opt/work/p",
    )
    updated = binding_store.apply_binding(
        project_id=project_id,
        host_id=_uid("host-a"),
        name="primary",
        repository_id=repo_id,
        workspace="/opt/work/p2",
    )
    assert updated.revision == 2
    assert updated.workspace == "/opt/work/p2"
    assert updated.updated_at is not None


def test_binding_code_repository_enabled_binding_is_primary(
    binding_store: SqlAlchemyProjectHostBindingStore,
    repository_store: SqlAlchemyProjectRepositoryStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """One enabled code-repository binding per host becomes primary."""
    project_id = _create_project(project_store)
    repo_id = _create_repo(repository_store, project_id, role="code")
    binding = binding_store.apply_binding(
        project_id=project_id,
        host_id=_uid("host-a"),
        name="primary",
        repository_id=repo_id,
        workspace="/w1",
    )
    assert binding.is_primary is True


def test_binding_second_enabled_duplicate_keeps_the_older_primary(
    binding_store: SqlAlchemyProjectHostBindingStore,
    repository_store: SqlAlchemyProjectRepositoryStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """The oldest enabled candidate stays primary; a newer one stays clear."""
    project_id = _create_project(project_store)
    repo_id = _create_repo(repository_store, project_id, role="code")
    first = binding_store.apply_binding(
        project_id=project_id,
        host_id=_uid("host-a"),
        name="primary",
        repository_id=repo_id,
        workspace="/w1",
    )
    second = binding_store.apply_binding(
        project_id=project_id,
        host_id=_uid("host-a"),
        name="extra",
        repository_id=repo_id,
        workspace="/w2",
    )
    assert binding_store.get(first.id).is_primary is True
    assert binding_store.get(second.id).is_primary is False


def test_disabled_older_duplicate_keeps_the_enabled_one_primary(
    binding_store: SqlAlchemyProjectHostBindingStore,
    repository_store: SqlAlchemyProjectRepositoryStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """A disabled candidate never displaces the enabled one."""
    project_id = _create_project(project_store)
    repo_id = _create_repo(repository_store, project_id, role="code")
    disabled = binding_store.apply_binding(
        project_id=project_id,
        host_id=_uid("host-a"),
        name="old",
        repository_id=repo_id,
        workspace="/w1",
        enabled=False,
    )
    enabled = binding_store.apply_binding(
        project_id=project_id,
        host_id=_uid("host-a"),
        name="primary",
        repository_id=repo_id,
        workspace="/w2",
    )
    assert binding_store.get(enabled.id).is_primary is True
    assert binding_store.get(disabled.id).is_primary is False

    # Any later write re-derives from the candidates: the enabled row stays.
    binding_store.apply_binding(
        project_id=project_id,
        host_id=_uid("host-a"),
        name="old",
        repository_id=repo_id,
        workspace="/w1-moved",
        enabled=False,
    )
    assert binding_store.get(enabled.id).is_primary is True
    assert binding_store.get(disabled.id).is_primary is False


def test_disabling_the_primary_clears_it(
    binding_store: SqlAlchemyProjectHostBindingStore,
    repository_store: SqlAlchemyProjectRepositoryStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """With no enabled candidate the host has no primary."""
    project_id = _create_project(project_store)
    repo_id = _create_repo(repository_store, project_id, role="code")
    host_id = _uid("host-a")
    binding = binding_store.apply_binding(
        project_id=project_id,
        host_id=host_id,
        name="primary",
        repository_id=repo_id,
        workspace="/w1",
    )
    assert binding.is_primary is True

    disabled = binding_store.apply_binding(
        project_id=project_id,
        host_id=host_id,
        name="primary",
        repository_id=repo_id,
        workspace="/w1",
        enabled=False,
    )
    assert disabled.is_primary is False


def test_primary_is_derived_per_host(
    binding_store: SqlAlchemyProjectHostBindingStore,
    repository_store: SqlAlchemyProjectRepositoryStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """Each host holds its own primary — the rule is per ``(project, host)``."""
    project_id = _create_project(project_store)
    repo_id = _create_repo(repository_store, project_id, role="code")
    a = binding_store.apply_binding(
        project_id=project_id,
        host_id=_uid("host-a"),
        name="primary",
        repository_id=repo_id,
        workspace="/w-a",
    )
    b = binding_store.apply_binding(
        project_id=project_id,
        host_id=_uid("host-b"),
        name="primary",
        repository_id=repo_id,
        workspace="/w-b",
    )
    assert a.is_primary is True
    assert b.is_primary is True
    assert len(binding_store.list_by_project(project_id)) == 2


def test_binding_get_list_delete(
    binding_store: SqlAlchemyProjectHostBindingStore,
    repository_store: SqlAlchemyProjectRepositoryStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """``get`` / ``list_by_project`` / ``list_by_host`` / ``delete`` round-trip."""
    project_id = _create_project(project_store)
    repo_id = _create_repo(repository_store, project_id)
    host_id = _uid("host-a")
    binding = binding_store.apply_binding(
        project_id=project_id,
        host_id=host_id,
        name="primary",
        repository_id=repo_id,
        workspace="/w",
    )
    assert binding_store.get(binding.id) == binding
    assert binding_store.get(_uid("nope")) is None
    assert [b.id for b in binding_store.list_by_project(project_id)] == [binding.id]
    assert [b.id for b in binding_store.list_by_host(project_id=project_id, host_id=host_id)] == [
        binding.id
    ]
    assert binding_store.list_by_host(project_id=project_id, host_id=_uid("host-b")) == []
    assert binding_store.delete_binding(project_id, host_id, "primary") is True
    assert binding_store.delete_binding(project_id, host_id, "primary") is False


def test_delete_binding_re_derives_the_next_primary(
    binding_store: SqlAlchemyProjectHostBindingStore,
    repository_store: SqlAlchemyProjectRepositoryStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """Removing the primary promotes the remaining enabled candidate."""
    project_id = _create_project(project_store)
    repo_id = _create_repo(repository_store, project_id, role="code")
    host_id = _uid("host-a")
    first = binding_store.apply_binding(
        project_id=project_id,
        host_id=host_id,
        name="primary",
        repository_id=repo_id,
        workspace="/w1",
    )
    second = binding_store.apply_binding(
        project_id=project_id,
        host_id=host_id,
        name="extra",
        repository_id=repo_id,
        workspace="/w2",
    )
    assert first.is_primary is True

    assert binding_store.delete_binding(project_id, host_id, "primary") is True
    promoted = binding_store.get(second.id)
    assert promoted is not None
    assert promoted.is_primary is True
    assert promoted.revision == 2


def test_demoting_the_code_repository_clears_primaries(
    binding_store: SqlAlchemyProjectHostBindingStore,
    repository_store: SqlAlchemyProjectRepositoryStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """A project that loses its only repository has no primary left."""
    project_id = _create_project(project_store)
    repo_id = _create_repo(repository_store, project_id, role="code")
    binding = binding_store.apply_binding(
        project_id=project_id,
        host_id=_uid("host-a"),
        name="primary",
        repository_id=repo_id,
        workspace="/w",
    )
    assert binding.is_primary is True
    # The store refuses deleting a referenced repository; a repository change
    # to related is the reachable path and clears the primary.
    repository_store.apply_repository(
        project_id=project_id,
        name="root",
        remote_url="git@github.com:example/repo.git",
        default_branch="main",
        role="related",
    )
    cleared = binding_store.get(binding.id)
    assert cleared is not None
    assert cleared.is_primary is False


# ── missing project ───────────────────────────────────────────────────


def test_repository_upsert_missing_project_raises_not_found(
    repository_store: SqlAlchemyProjectRepositoryStore,
) -> None:
    """Upserting on an unknown project raises ``NOT_FOUND`` and writes no row."""
    missing = _uid("missing-proj")
    with pytest.raises(OmnigentError) as exc:
        repository_store.apply_repository(
            project_id=missing,
            name="root",
            remote_url="u",
            default_branch="main",
        )
    assert exc.value.code == ErrorCode.NOT_FOUND
    assert missing in exc.value.message
    assert repository_store.get_by_name(project_id=missing, name="root") is None
    assert repository_store.list_by_project(missing) == []


def test_binding_upsert_missing_project_raises_not_found(
    binding_store: SqlAlchemyProjectHostBindingStore,
) -> None:
    """Upserting on an unknown project raises ``NOT_FOUND`` and writes no row."""
    missing = _uid("missing-proj")
    with pytest.raises(OmnigentError) as exc:
        binding_store.apply_binding(
            project_id=missing,
            host_id=_uid("host-a"),
            name="primary",
            repository_id=_uid("repo"),
            workspace="/w",
        )
    assert exc.value.code == ErrorCode.NOT_FOUND
    assert missing in exc.value.message
    assert binding_store.list_by_project(missing) == []
    assert binding_store.list_by_host(project_id=missing, host_id=_uid("host-a")) == []


def test_repository_delete_missing_project_raises_not_found(
    repository_store: SqlAlchemyProjectRepositoryStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """Deleting a row whose project is gone raises ``NOT_FOUND``, not ``False``."""
    project_id = _create_project(project_store)
    repo, _ = repository_store.apply_repository(
        project_id=project_id,
        name="root",
        remote_url="u",
        default_branch="main",
    )
    assert project_store.delete(project_id, user_id="alice@example.com") is True
    with pytest.raises(OmnigentError) as exc:
        repository_store.delete(repo.id)
    assert exc.value.code == ErrorCode.NOT_FOUND
    assert project_id in exc.value.message


def test_binding_delete_missing_project_raises_not_found(
    binding_store: SqlAlchemyProjectHostBindingStore,
    repository_store: SqlAlchemyProjectRepositoryStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """Deleting a row whose project is gone raises ``NOT_FOUND``, not ``False``."""
    project_id = _create_project(project_store)
    repo_id = _create_repo(repository_store, project_id)
    host_id = _uid("host-a")
    binding_store.apply_binding(
        project_id=project_id,
        host_id=host_id,
        name="primary",
        repository_id=repo_id,
        workspace="/w",
    )
    assert project_store.delete(project_id, user_id="alice@example.com") is True
    with pytest.raises(OmnigentError) as exc:
        binding_store.delete_binding(project_id, host_id, "primary")
    assert exc.value.code == ErrorCode.NOT_FOUND
    assert project_id in exc.value.message


# ── record_verification ─────────────────────────────────────────────────


def _create_verified_binding(
    binding_store: SqlAlchemyProjectHostBindingStore,
    repository_store: SqlAlchemyProjectRepositoryStore,
    project_store: SqlAlchemyProjectStore,
    seed: str = "proj",
):
    """Create a project, repository and verified binding; return all three ids."""
    project_id = _create_project(project_store, seed)
    repo_id = _create_repo(repository_store, project_id)
    binding = binding_store.apply_binding(
        project_id=project_id,
        host_id=_uid("host-a"),
        name="primary",
        repository_id=repo_id,
        workspace="/w",
    )
    return project_id, repo_id, binding


def test_record_verification_concurrent_disable_returns_none(
    binding_store: SqlAlchemyProjectHostBindingStore,
    repository_store: SqlAlchemyProjectRepositoryStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """A concurrent disable wins: the stale verification applies nothing."""
    project_id, repo_id, binding = _create_verified_binding(
        binding_store, repository_store, project_store
    )
    stale_revision = binding.revision
    disabled = binding_store.apply_binding(
        project_id=project_id,
        host_id=_uid("host-a"),
        name="primary",
        repository_id=repo_id,
        workspace="/w",
        enabled=False,
    )
    assert (
        binding_store.record_verification(
            binding.id,
            expected_revision=stale_revision,
            workspace="/w",
            path_verified_at=200,
        )
        is None
    )
    after = binding_store.get(binding.id)
    assert after is not None
    assert after.enabled is False
    assert after.revision == stale_revision + 1
    assert after.path_verified_at == disabled.path_verified_at


def test_record_verification_after_delete_returns_none(
    binding_store: SqlAlchemyProjectHostBindingStore,
    repository_store: SqlAlchemyProjectRepositoryStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """Verifying a deleted binding returns ``None`` and recreates no row."""
    project_id, _repo_id, binding = _create_verified_binding(
        binding_store, repository_store, project_store
    )
    stale_revision = binding.revision
    assert binding_store.delete_binding(project_id, _uid("host-a"), "primary") is True
    assert (
        binding_store.record_verification(
            binding.id,
            expected_revision=stale_revision,
            workspace="/w",
            path_verified_at=200,
        )
        is None
    )
    assert binding_store.get(binding.id) is None
    assert binding_store.list_by_project(project_id) == []


def test_record_verification_unchanged_path_keeps_revision(
    binding_store: SqlAlchemyProjectHostBindingStore,
    repository_store: SqlAlchemyProjectRepositoryStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """A re-verify of the same path stamps the time without bumping."""
    _project_id, _repo_id, binding = _create_verified_binding(
        binding_store, repository_store, project_store
    )
    refreshed = binding_store.record_verification(
        binding.id,
        expected_revision=binding.revision,
        workspace="/w",
        path_verified_at=200,
    )
    assert refreshed is not None
    assert refreshed.revision == binding.revision
    assert refreshed.workspace == "/w"
    assert refreshed.path_verified_at == 200
    assert refreshed.updated_at is not None


def test_record_verification_moved_path_bumps_once(
    binding_store: SqlAlchemyProjectHostBindingStore,
    repository_store: SqlAlchemyProjectRepositoryStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """A moved canonical path is stored with exactly one revision bump."""
    _project_id, _repo_id, binding = _create_verified_binding(
        binding_store, repository_store, project_store
    )
    moved = binding_store.record_verification(
        binding.id,
        expected_revision=binding.revision,
        workspace="/w-canonical",
        path_verified_at=200,
    )
    assert moved is not None
    assert moved.workspace == "/w-canonical"
    assert moved.revision == binding.revision + 1
    again = binding_store.record_verification(
        binding.id,
        expected_revision=moved.revision,
        workspace="/w-canonical",
        path_verified_at=200,
    )
    assert again is not None
    assert again.revision == moved.revision


# ── cross-store integrity ───────────────────────────────────────────────


def test_repository_delete_with_binding_conflicts(
    binding_store: SqlAlchemyProjectHostBindingStore,
    repository_store: SqlAlchemyProjectRepositoryStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """A referenced repository cannot be deleted, and it survives the attempt."""
    project_id, repo_id, _binding = _create_verified_binding(
        binding_store, repository_store, project_store
    )
    repo = repository_store.get(repo_id)
    assert repo is not None
    with pytest.raises(OmnigentError) as exc:
        repository_store.delete(repo_id)
    assert exc.value.code == ErrorCode.CONFLICT
    assert repo.name in exc.value.message
    assert "1 binding(s)" in exc.value.message
    assert repository_store.get(repo_id) is not None
    assert [r.id for r in repository_store.list_by_project(project_id)] == [repo_id]


def test_binding_upsert_unknown_repository_rejected(
    binding_store: SqlAlchemyProjectHostBindingStore,
    repository_store: SqlAlchemyProjectRepositoryStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """A binding naming no repository raises ``INVALID_INPUT`` and writes nothing."""
    project_id = _create_project(project_store)
    missing = _uid("missing-repo")
    with pytest.raises(OmnigentError) as exc:
        binding_store.apply_binding(
            project_id=project_id,
            host_id=_uid("host-a"),
            name="primary",
            repository_id=missing,
            workspace="/w",
        )
    assert exc.value.code == ErrorCode.INVALID_INPUT
    assert missing in exc.value.message
    assert binding_store.list_by_project(project_id) == []


def test_binding_upsert_deleted_repository_rejected(
    binding_store: SqlAlchemyProjectHostBindingStore,
    repository_store: SqlAlchemyProjectRepositoryStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """A binding naming a deleted repository raises ``INVALID_INPUT``."""
    project_id = _create_project(project_store)
    repo_id = _create_repo(repository_store, project_id)
    assert repository_store.delete(repo_id) is True
    with pytest.raises(OmnigentError) as exc:
        binding_store.apply_binding(
            project_id=project_id,
            host_id=_uid("host-a"),
            name="primary",
            repository_id=repo_id,
            workspace="/w",
        )
    assert exc.value.code == ErrorCode.INVALID_INPUT
    assert repo_id in exc.value.message
    assert binding_store.list_by_project(project_id) == []


def test_binding_upsert_foreign_repository_rejected(
    binding_store: SqlAlchemyProjectHostBindingStore,
    repository_store: SqlAlchemyProjectRepositoryStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """A binding naming another project's repository raises ``INVALID_INPUT``."""
    project_id = _create_project(project_store, "proj")
    other_id = _create_project(project_store, "other", name="Q")
    foreign_repo_id = _create_repo(repository_store, other_id)
    with pytest.raises(OmnigentError) as exc:
        binding_store.apply_binding(
            project_id=project_id,
            host_id=_uid("host-a"),
            name="primary",
            repository_id=foreign_repo_id,
            workspace="/w",
        )
    assert exc.value.code == ErrorCode.INVALID_INPUT
    assert foreign_repo_id in exc.value.message
    assert binding_store.list_by_project(project_id) == []


def test_repository_delete_after_binding_removal_succeeds(
    binding_store: SqlAlchemyProjectHostBindingStore,
    repository_store: SqlAlchemyProjectRepositoryStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """Deleting the last binding unblocks the repository delete."""
    project_id = _create_project(project_store)
    repo_id = _create_repo(repository_store, project_id)
    binding_store.apply_binding(
        project_id=project_id,
        host_id=_uid("host-a"),
        name="primary",
        repository_id=repo_id,
        workspace="/w",
    )
    assert binding_store.delete_binding(project_id, _uid("host-a"), "primary") is True
    assert repository_store.delete(repo_id) is True


# ── entries ─────────────────────────────────────────────────────────────


def test_entry_put_inserts_and_lists_ordered(
    binding_store: SqlAlchemyProjectHostBindingStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """Entries key on (project, host): inserted per host, listed by host id."""
    project_id = _create_project(project_store)
    first = binding_store.put_entry(project_id, _uid("host-b"), "/w/b")
    assert first.created_at > 0
    assert first.updated_at is None
    binding_store.put_entry(project_id, _uid("host-a"), "/w/a")
    assert [entry.host_id for entry in binding_store.list_entries(project_id)] == [
        _uid("host-a"),
        _uid("host-b"),
    ]
    assert binding_store.list_entries(_uid("other")) == []
    assert first == binding_store.put_entry(project_id, _uid("host-b"), "/w/b")


def test_entry_put_change_moves_and_stamps_updated_at(
    binding_store: SqlAlchemyProjectHostBindingStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """A moved path is stored and stamped; the identical put is a no-op."""
    project_id = _create_project(project_store)
    host_id = _uid("host-a")
    created = binding_store.put_entry(project_id, host_id, "/w")
    moved = binding_store.put_entry(project_id, host_id, "/w-moved")
    assert moved.workspace == "/w-moved"
    assert moved.created_at == created.created_at
    assert moved.updated_at is not None
    unchanged = binding_store.put_entry(project_id, host_id, "/w-moved")
    assert unchanged == moved


def test_entry_delete_is_idempotent(
    binding_store: SqlAlchemyProjectHostBindingStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """Delete removes the row once and reports absence afterwards."""
    project_id = _create_project(project_store)
    host_id = _uid("host-a")
    binding_store.put_entry(project_id, host_id, "/w")
    assert binding_store.delete_entry(project_id, host_id) is True
    assert binding_store.delete_entry(project_id, host_id) is False
    assert binding_store.list_entries(project_id) == []


def test_entry_at_or_under_is_tenant_scoped_and_project_agnostic(
    binding_store: SqlAlchemyProjectHostBindingStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """The guard matches host + workspace, whatever project owns the entry."""
    mine = _create_project(project_store, "proj")
    other = _create_project(project_store, "other", name="Q")
    host_id = _uid("host-a")
    binding_store.put_entry(other, host_id, "/entry")
    assert binding_store.entry_at_or_under(host_id, "/entry") is True
    assert binding_store.entry_at_or_under(host_id, "/elsewhere") is False
    assert binding_store.entry_at_or_under(_uid("host-b"), "/entry") is False
    assert binding_store.list_entries(mine) == []


def test_entry_at_or_under_matches_nested_entries(
    binding_store: SqlAlchemyProjectHostBindingStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """An entry inside the directory keeps the whole directory, trailing slash included."""
    project_id = _create_project(project_store)
    host_id = _uid("host-a")
    binding_store.put_entry(project_id, host_id, "/repo-worktrees/topic/subproject")
    assert binding_store.entry_at_or_under(host_id, "/repo-worktrees/topic") is True
    assert binding_store.entry_at_or_under(host_id, "/repo-worktrees/topic/") is True
    assert binding_store.entry_at_or_under(host_id, "/repo-worktrees") is True
    assert binding_store.entry_at_or_under(host_id, "/repo") is False


def test_entry_at_or_under_rejects_a_sibling_prefix(
    binding_store: SqlAlchemyProjectHostBindingStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """``/a/bc`` is not inside ``/a/b``; the separator decides, not the string."""
    project_id = _create_project(project_store)
    host_id = _uid("host-a")
    binding_store.put_entry(project_id, host_id, "/a/bc")
    assert binding_store.entry_at_or_under(host_id, "/a/b") is False
    assert binding_store.entry_at_or_under(host_id, "/a") is True


def test_entry_at_or_under_treats_wildcards_literally(
    binding_store: SqlAlchemyProjectHostBindingStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """``%`` and ``_`` in a path are characters, never LIKE wildcards."""
    project_id = _create_project(project_store)
    underscore_host = _uid("host-underscore")
    binding_store.put_entry(project_id, underscore_host, "/a/bXc/d")
    assert binding_store.entry_at_or_under(underscore_host, "/a/b_c") is False
    assert binding_store.entry_at_or_under(underscore_host, "/a/bXc") is True
    percent_host = _uid("host-percent")
    binding_store.put_entry(project_id, percent_host, "/a/bX/c")
    assert binding_store.entry_at_or_under(percent_host, "/a/%") is False
    assert binding_store.entry_at_or_under(percent_host, "/a/bX") is True


def test_entry_at_or_under_accepts_windows_separators(
    binding_store: SqlAlchemyProjectHostBindingStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """A backslash path matches at and under it, trailing separator included."""
    project_id = _create_project(project_store)
    host_id = _uid("host-a")
    binding_store.put_entry(project_id, host_id, "D:\\repo\\wt\\sub")
    assert binding_store.entry_at_or_under(host_id, "D:\\repo\\wt") is True
    assert binding_store.entry_at_or_under(host_id, "D:\\repo\\wt\\") is True
    assert binding_store.entry_at_or_under(host_id, "D:\\repo\\wo") is False


def test_entry_put_missing_project_raises_not_found(
    binding_store: SqlAlchemyProjectHostBindingStore,
) -> None:
    """Upserting an entry on an unknown project writes no orphan row."""
    missing = _uid("missing-proj")
    with pytest.raises(OmnigentError) as exc:
        binding_store.put_entry(missing, _uid("host-a"), "/w")
    assert exc.value.code == ErrorCode.NOT_FOUND
    assert missing in exc.value.message
    assert binding_store.list_entries(missing) == []


def test_entry_delete_missing_project_raises_not_found(
    binding_store: SqlAlchemyProjectHostBindingStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """Deleting a row whose project is gone raises ``NOT_FOUND``, not ``False``."""
    project_id = _create_project(project_store)
    host_id = _uid("host-a")
    binding_store.put_entry(project_id, host_id, "/w")
    assert project_store.delete(project_id, user_id="alice@example.com") is True
    with pytest.raises(OmnigentError) as exc:
        binding_store.delete_entry(project_id, host_id)
    assert exc.value.code == ErrorCode.NOT_FOUND
    assert project_id in exc.value.message
