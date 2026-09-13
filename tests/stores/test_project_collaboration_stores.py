"""Tests for collaboration config and the repository/binding stores.

Covers ``ProjectStore.set_collaboration`` (revision bump and conflict),
repository upsert (revision bump on change only), and binding upsert (the
one-primary invariant per ``(project, host)``).
"""

from __future__ import annotations

import uuid

import pytest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.stores.project_host_binding_store import DuplicatePrimaryBindingError
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
) -> str:
    """Register a repository and return its id (bindings must name a real one)."""
    return repository_store.upsert(
        project_id=project_id,
        name=name,
        remote_url="git@github.com:example/repo.git",
        default_branch="main",
    ).id


# ── set_collaboration ───────────────────────────────────────────────────


def test_set_collaboration_enables_and_bumps_revision(
    project_store: SqlAlchemyProjectStore,
) -> None:
    """Enabling with the current revision flips the switch and bumps to 1."""
    project_store.create(_uid("p1"), "P", "alice@example.com")
    updated = project_store.set_collaboration(
        _uid("p1"), user_id="alice@example.com", enabled=True, expected_revision=0
    )
    assert updated is not None
    assert updated.collaboration_enabled is True
    assert updated.collaboration_revision == 1
    assert updated.updated_at is not None


def test_set_collaboration_stale_revision_conflicts(
    project_store: SqlAlchemyProjectStore,
) -> None:
    """A second writer holding the old revision gets ``CONFLICT``."""
    project_store.create(_uid("p1"), "P", "alice@example.com")
    project_store.set_collaboration(
        _uid("p1"), user_id="alice@example.com", enabled=True, expected_revision=0
    )
    with pytest.raises(OmnigentError) as exc:
        project_store.set_collaboration(
            _uid("p1"), user_id="alice@example.com", enabled=False, expected_revision=0
        )
    assert exc.value.code == ErrorCode.CONFLICT
    # The winner's write stands.
    got = project_store.get(_uid("p1"), user_id="alice@example.com")
    assert got is not None
    assert (got.collaboration_enabled, got.collaboration_revision) == (True, 1)


def test_set_collaboration_missing_returns_none(
    project_store: SqlAlchemyProjectStore,
) -> None:
    """An unknown project returns ``None``, like ``update``."""
    assert (
        project_store.set_collaboration(
            _uid("nope"), user_id="alice@example.com", enabled=True, expected_revision=0
        )
        is None
    )


def test_set_collaboration_scoped_to_owner(project_store: SqlAlchemyProjectStore) -> None:
    """A non-owner cannot flip another user's switch."""
    project_store.create(_uid("p1"), "P", "alice@example.com")
    assert (
        project_store.set_collaboration(
            _uid("p1"), user_id="bob@example.com", enabled=True, expected_revision=0
        )
        is None
    )
    assert (
        project_store.get(_uid("p1"), user_id="alice@example.com").collaboration_enabled is False
    )


def test_new_projects_start_uncollaborative(project_store: SqlAlchemyProjectStore) -> None:
    """The migrated columns default to disabled / revision 0."""
    project = project_store.create(_uid("p1"), "P", "alice@example.com")
    assert project.collaboration_enabled is False
    assert project.collaboration_revision == 0


# ── repositories ────────────────────────────────────────────────────────


def test_repository_upsert_inserts_at_revision_1(
    repository_store: SqlAlchemyProjectRepositoryStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """A new registration starts at revision 1."""
    _create_project(project_store)
    repo = repository_store.upsert(
        project_id=_uid("proj"),
        name="root",
        remote_url="git@github.com:example/repo.git",
        default_branch="main",
    )
    assert repo.revision == 1
    assert repo.context_manifest_path == ".agents/project/manifest.json"
    assert repo.created_at > 0
    assert repo.updated_at is None


def test_repository_upsert_identical_is_noop(
    repository_store: SqlAlchemyProjectRepositoryStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """Re-registering unchanged values returns the row without a bump."""
    _create_project(project_store)
    first = repository_store.upsert(
        project_id=_uid("proj"),
        name="root",
        remote_url="git@github.com:example/repo.git",
        default_branch="main",
    )
    second = repository_store.upsert(
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
    repository_store.upsert(
        project_id=_uid("proj"),
        name="root",
        remote_url="git@github.com:example/repo.git",
        default_branch="main",
    )
    updated = repository_store.upsert(
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
    repo = repository_store.upsert(
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


# ── bindings ────────────────────────────────────────────────────────────


def test_binding_upsert_inserts_at_revision_1(
    binding_store: SqlAlchemyProjectHostBindingStore,
    repository_store: SqlAlchemyProjectRepositoryStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """A new binding starts at revision 1, non-primary by default."""
    project_id = _create_project(project_store)
    repo_id = _create_repo(repository_store, project_id)
    binding = binding_store.upsert(
        project_id=project_id,
        host_id=_uid("host-a"),
        name="primary",
        repository_id=repo_id,
        workspace="/Users/dev/work/p",
    )
    assert binding.revision == 1
    assert binding.is_primary is False
    assert binding.enabled is True
    assert binding.path_verified_at is None


def test_binding_upsert_change_bumps_revision(
    binding_store: SqlAlchemyProjectHostBindingStore,
    repository_store: SqlAlchemyProjectRepositoryStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """A changed path bumps the revision."""
    project_id = _create_project(project_store)
    repo_id = _create_repo(repository_store, project_id)
    binding_store.upsert(
        project_id=project_id,
        host_id=_uid("host-a"),
        name="primary",
        repository_id=repo_id,
        workspace="/Users/dev/work/p",
    )
    updated = binding_store.upsert(
        project_id=project_id,
        host_id=_uid("host-a"),
        name="primary",
        repository_id=repo_id,
        workspace="/Users/dev/work/p2",
    )
    assert updated.revision == 2
    assert updated.workspace == "/Users/dev/work/p2"
    assert updated.updated_at is not None


def test_second_primary_on_same_host_rejected(
    binding_store: SqlAlchemyProjectHostBindingStore,
    repository_store: SqlAlchemyProjectRepositoryStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """A second primary for one ``(project, host)`` is rejected; the old stays."""
    project_id = _create_project(project_store)
    repo_id = _create_repo(repository_store, project_id)
    first = binding_store.upsert(
        project_id=project_id,
        host_id=_uid("host-a"),
        name="primary",
        repository_id=repo_id,
        workspace="/w1",
        is_primary=True,
    )
    with pytest.raises(DuplicatePrimaryBindingError):
        binding_store.upsert(
            project_id=project_id,
            host_id=_uid("host-a"),
            name="other",
            repository_id=repo_id,
            workspace="/w2",
            is_primary=True,
        )
    # The existing primary is untouched — never silently cleared.
    assert binding_store.get(first.id) is not None
    assert binding_store.get(first.id).is_primary is True
    assert [
        b.name for b in binding_store.list_by_host(project_id=_uid("proj"), host_id=_uid("host-a"))
    ] == ["primary"]


def test_promoting_to_second_primary_rejected(
    binding_store: SqlAlchemyProjectHostBindingStore,
    repository_store: SqlAlchemyProjectRepositoryStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """Flipping a non-primary row to primary while one exists is rejected."""
    project_id = _create_project(project_store)
    repo_id = _create_repo(repository_store, project_id)
    binding_store.upsert(
        project_id=project_id,
        host_id=_uid("host-a"),
        name="primary",
        repository_id=repo_id,
        workspace="/w1",
        is_primary=True,
    )
    second = binding_store.upsert(
        project_id=project_id,
        host_id=_uid("host-a"),
        name="other",
        repository_id=repo_id,
        workspace="/w2",
    )
    with pytest.raises(DuplicatePrimaryBindingError):
        binding_store.upsert(
            project_id=project_id,
            host_id=_uid("host-a"),
            name="other",
            repository_id=repo_id,
            workspace="/w2",
            is_primary=True,
        )
    assert binding_store.get(second.id).revision == 1


def test_primary_on_different_host_allowed(
    binding_store: SqlAlchemyProjectHostBindingStore,
    repository_store: SqlAlchemyProjectRepositoryStore,
    project_store: SqlAlchemyProjectStore,
) -> None:
    """Each host holds its own primary — the invariant is per ``(project, host)``."""
    project_id = _create_project(project_store)
    repo_id = _create_repo(repository_store, project_id)
    a = binding_store.upsert(
        project_id=project_id,
        host_id=_uid("host-a"),
        name="primary",
        repository_id=repo_id,
        workspace=r"C:\work\p",
        is_primary=True,
    )
    b = binding_store.upsert(
        project_id=project_id,
        host_id=_uid("host-b"),
        name="primary",
        repository_id=repo_id,
        workspace="/Users/dev/work/p",
        is_primary=True,
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
    binding = binding_store.upsert(
        project_id=project_id,
        host_id=_uid("host-a"),
        name="primary",
        repository_id=repo_id,
        workspace="/w",
    )
    assert binding_store.get(binding.id) == binding
    assert binding_store.get(_uid("nope")) is None
    assert [b.id for b in binding_store.list_by_project(project_id)] == [binding.id]
    assert [
        b.id for b in binding_store.list_by_host(project_id=project_id, host_id=_uid("host-a"))
    ] == [binding.id]
    assert binding_store.list_by_host(project_id=project_id, host_id=_uid("host-b")) == []
    assert binding_store.delete(binding.id) is True
    assert binding_store.delete(binding.id) is False


# ── missing project ───────────────────────────────────────────────────


def test_repository_upsert_missing_project_raises_not_found(
    repository_store: SqlAlchemyProjectRepositoryStore,
) -> None:
    """Upserting on an unknown project raises ``NOT_FOUND`` and writes no row."""
    missing = _uid("missing-proj")
    with pytest.raises(OmnigentError) as exc:
        repository_store.upsert(
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
        binding_store.upsert(
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
    repo = repository_store.upsert(
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
    binding = binding_store.upsert(
        project_id=project_id,
        host_id=_uid("host-a"),
        name="primary",
        repository_id=repo_id,
        workspace="/w",
    )
    assert project_store.delete(project_id, user_id="alice@example.com") is True
    with pytest.raises(OmnigentError) as exc:
        binding_store.delete(binding.id)
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
    binding = binding_store.upsert(
        project_id=project_id,
        host_id=_uid("host-a"),
        name="primary",
        repository_id=repo_id,
        workspace="/w",
        path_verified_at=100,
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
    binding_store.upsert(
        project_id=project_id,
        host_id=_uid("host-a"),
        name="primary",
        repository_id=repo_id,
        workspace="/w",
        enabled=False,
        path_verified_at=100,
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
    assert after.path_verified_at == 100


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
    assert binding_store.delete(binding.id) is True
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
        binding_store.upsert(
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
        binding_store.upsert(
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
        binding_store.upsert(
            project_id=project_id,
            host_id=_uid("host-a"),
            name="primary",
            repository_id=foreign_repo_id,
            workspace="/w",
        )
    assert exc.value.code == ErrorCode.INVALID_INPUT
    assert foreign_repo_id in exc.value.message
    assert binding_store.list_by_project(project_id) == []
