"""Real callback boundaries for Omnigent-owned Git, separate from user commands."""

import ast
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from omnigent import git_commands
from omnigent.host.git_worktree import read_worktree_status


@pytest.fixture
def callback_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in list(os.environ):
        if name.upper().startswith("GIT_"):
            monkeypatch.delenv(name)
    template = tmp_path / "template"
    template.mkdir()
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_TEMPLATE_DIR", str(template))
    monkeypatch.chdir(tmp_path)
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Probe"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "probe@example.test"], check=True
    )
    (repo / "tracked").write_text("initial\n")
    subprocess.run(["git", "-C", str(repo), "add", "tracked"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"], check=True, capture_output=True
    )
    return repo


def _configure_callback(repo: Path, kind: str) -> Path:
    marker = repo / "callback-ran"
    script = repo / "callback.py"
    script.write_text(
        "from pathlib import Path\nimport sys\n"
        'Path("callback-ran").write_text("ran")\n'
        + (
            "sys.stdout.buffer.write(sys.stdin.buffer.read())\n"
            if kind == "clean"
            else 'sys.stdout.buffer.write(b"token\\0")\n'
        )
    )
    key = "filter.example.clean" if kind == "clean" else "core.fsmonitor"
    subprocess.run(
        ["git", "-C", str(repo), "config", key, shlex.join([sys.executable, str(script)])],
        check=True,
    )
    if kind == "clean":
        (repo / ".gitattributes").write_text("tracked filter=example\n")
    (repo / "tracked").write_text("changed\n")
    return marker


@pytest.mark.parametrize("kind", ["clean", "fsmonitor"])
def test_omnigent_status_does_not_run_repo_callbacks(callback_repo: Path, kind: str) -> None:
    marker = _configure_callback(callback_repo, kind)
    read_worktree_status(str(callback_repo))
    assert not marker.exists(), f"Omnigent status executed repository {kind}"


def test_checkout_hook_is_disabled_only_for_internal_git(callback_repo: Path) -> None:
    hook = callback_repo / ".git" / "hooks" / "post-checkout"
    hook.parent.mkdir()
    hook.write_text("#!/bin/sh\nprintf ran > hook-ran\n")
    hook.chmod(0o755)
    subprocess.run(
        ["git", "-C", str(callback_repo), "checkout", "-b", "user"],
        check=True,
        capture_output=True,
    )
    marker = callback_repo / "hook-ran"
    assert marker.exists(), "User Git positive control must execute the hook"
    marker.unlink()
    git_commands.run_git(
        ["git", "-C", str(callback_repo), "checkout", "-b", "internal"],
        check=True,
        capture_output=True,
    )
    assert not marker.exists()


@pytest.mark.parametrize("key", ["diff.external", "diff.example.command", "diff.example.textconv"])
def test_diff_callbacks_are_disabled(callback_repo: Path, key: str) -> None:
    marker = _configure_callback(callback_repo, "clean")
    (callback_repo / ".gitattributes").write_text("tracked diff=example\n")
    script = callback_repo / "callback.py"
    subprocess.run(
        [
            "git",
            "-C",
            str(callback_repo),
            "config",
            key,
            shlex.join([sys.executable, str(script)]),
        ],
        check=True,
    )
    result = git_commands.run_git(
        ["git", "diff"], cwd=callback_repo, check=True, capture_output=True
    )
    assert b"changed" in result.stdout
    assert not marker.exists()


def test_required_filter_can_be_neutralized_without_failing_status(callback_repo: Path) -> None:
    marker = _configure_callback(callback_repo, "clean")
    subprocess.run(
        ["git", "-C", str(callback_repo), "config", "filter.example.required", "true"], check=True
    )
    subprocess.run(
        ["git", "-C", str(callback_repo), "config", "filter.example.process", "must-not-start"],
        check=True,
    )
    git_commands.run_git(
        ["git", "status", "--porcelain"], cwd=callback_repo, check=True, capture_output=True
    )
    assert not marker.exists()


@pytest.mark.parametrize("scope", ["system", "global"])
def test_repo_credentials_are_reset_but_host_helper_is_retained(
    callback_repo: Path, monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    repo_marker = callback_repo / "repo-helper-ran"
    host_marker = callback_repo.parent / "host-helper-ran"
    script = callback_repo.parent / "host-helper.py"
    script.write_text(f"from pathlib import Path\nPath({str(host_marker)!r}).touch()\n")
    host_file = callback_repo.parent / "host.gitconfig"
    monkeypatch.setenv(f"GIT_CONFIG_{scope.upper()}", str(host_file))
    if scope == "system":
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "0")
    subprocess.run(
        [
            "git",
            "config",
            f"--{scope}",
            "credential.helper",
            "!" + shlex.join([sys.executable, str(script)]),
        ],
        check=True,
    )
    repo_script = callback_repo / "repo-helper.py"
    repo_script.write_text(f"from pathlib import Path\nPath({str(repo_marker)!r}).touch()\n")
    included_file = callback_repo.parent / "included.gitconfig"
    subprocess.run(
        [
            "git",
            "config",
            "--file",
            str(included_file),
            "credential.helper",
            "!" + shlex.join([sys.executable, str(repo_script)]),
        ],
        check=True,
    )
    subprocess.run(["git", "config", f"--{scope}", "include.path", str(included_file)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(callback_repo),
            "config",
            "credential.https://example.test.helper",
            "!" + shlex.join([sys.executable, str(repo_script)]),
        ],
        check=True,
    )
    git_commands.run_git(
        ["git", "credential", "fill"],
        cwd=callback_repo,
        input=b"protocol=https\nhost=example.test\n\n",
        capture_output=True,
    )
    assert host_marker.exists()
    assert not repo_marker.exists()


def test_valueless_command_credential_helper_is_ignored(callback_repo: Path) -> None:
    result = git_commands.run_git(
        ["git", "-c", "credential.helper", "rev-parse", "--is-inside-work-tree"],
        cwd=callback_repo,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "true"


@pytest.mark.parametrize("disabled_by", ["global_reset", "nosystem"])
def test_host_system_helper_respects_resets_and_nosystem(
    callback_repo: Path, monkeypatch: pytest.MonkeyPatch, disabled_by: str
) -> None:
    marker = callback_repo.parent / "system-helper-ran"
    system_file = callback_repo.parent / "system.gitconfig"
    system_file.write_text(f'[credential]\n\thelper = "!touch {marker}"\n')
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(system_file))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1" if disabled_by == "nosystem" else "0")
    if disabled_by == "global_reset":
        global_file = callback_repo.parent / "global.gitconfig"
        global_file.write_text('[credential "https://example.test"]\n\thelper =\n')
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_file))
    result = git_commands.run_git(
        ["git", "credential", "fill"],
        cwd=callback_repo,
        input=b"protocol=https\nhost=example.test\n\n",
        capture_output=True,
    )
    assert result.returncode != 0
    assert not marker.exists()


def test_guard_failure_never_runs_the_requested_command(
    callback_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import Mock

    def unavailable(*args, **kwargs):
        raise OSError("configuration unavailable")

    monkeypatch.setattr(git_commands, "_config_snapshot", unavailable)
    requested = Mock()
    monkeypatch.setattr(git_commands.subprocess, "run", requested)
    with pytest.raises(OSError, match="configuration unavailable"):
        git_commands.run_git(["git", "status"], cwd=callback_repo)
    requested.assert_not_called()


def test_module_entry_uses_the_same_guard(callback_repo: Path) -> None:
    marker = _configure_callback(callback_repo, "clean")
    shadow = callback_repo / "omnigent"
    shadow.mkdir()
    shadow_marker = callback_repo / "shadow-ran"
    (shadow / "__init__.py").write_text(
        f"from pathlib import Path; Path({str(shadow_marker)!r}).touch()\n"
    )
    subprocess.run(
        [
            sys.executable,
            "-I",
            "-m",
            "omnigent.git_commands",
            "--",
            "-C",
            str(callback_repo),
            "status",
            "--porcelain",
        ],
        check=True,
        capture_output=True,
        cwd=callback_repo,
        env={**os.environ, "PYTHONPATH": str(callback_repo)},
    )
    assert not marker.exists()
    assert not shadow_marker.exists()


def test_guard_does_not_widen_explicit_protocol_policy(callback_repo: Path) -> None:
    result = git_commands.run_git(
        [
            "git",
            "-c",
            "protocol.allow=never",
            "-c",
            "protocol.https.allow=always",
            "ls-remote",
            str(callback_repo),
        ],
        cwd=callback_repo,
        capture_output=True,
    )
    assert result.returncode != 0
    assert b"not allowed" in result.stderr


def test_remote_upload_pack_cannot_choose_a_repo_command(callback_repo: Path) -> None:
    marker = callback_repo / "upload-pack-ran"
    script = callback_repo.parent / "upload-pack"
    script.write_text(
        "#!/bin/sh\ntouch " + shlex.quote(str(marker)) + '\nexec git-upload-pack "$@"\n'
    )
    script.chmod(0o755)
    subprocess.run(
        ["git", "-C", str(callback_repo), "remote", "add", "origin", str(callback_repo)],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(callback_repo), "config", "remote.origin.uploadpack", str(script)],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(callback_repo), "ls-remote", "origin"], check=True, capture_output=True
    )
    assert marker.exists(), "User Git positive control must run configured upload-pack"
    marker.unlink()
    result = git_commands.run_git(
        ["git", "ls-remote", "origin"], cwd=callback_repo, check=True, capture_output=True
    )
    assert b"refs/heads/main" in result.stdout
    assert not marker.exists()


def test_worktree_add_snapshots_destination_conditional_filters(callback_repo: Path) -> None:
    marker = callback_repo.parent / "destination-filter-ran"
    included = callback_repo.parent / "included.gitconfig"
    callback = shlex.join(
        [
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).touch(); "
            "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
        ]
    )
    subprocess.run(
        ["git", "config", "--file", str(included), "filter.destination.smudge", callback],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(callback_repo),
            "config",
            "includeIf.gitdir:**/worktrees/**.path",
            str(included),
        ],
        check=True,
    )
    (callback_repo / ".gitattributes").write_text("tracked filter=destination\n")
    subprocess.run(["git", "-C", str(callback_repo), "add", ".gitattributes"], check=True)
    subprocess.run(
        ["git", "-C", str(callback_repo), "commit", "-m", "attributes"],
        check=True,
        capture_output=True,
    )
    raw = callback_repo.parent / "raw-linked"
    subprocess.run(
        ["git", "-C", str(callback_repo), "worktree", "add", "--detach", str(raw)],
        check=True,
        capture_output=True,
    )
    assert marker.exists(), "The pre-existing conditional driver runs on unguarded add"
    marker.unlink()
    guarded = callback_repo.parent / "guarded-linked"
    git_commands.run_git(
        ["git", "worktree", "add", "--detach", str(guarded)],
        cwd=callback_repo,
        check=True,
        capture_output=True,
    )
    assert (guarded / "tracked").read_text() == "initial\n"
    assert not marker.exists()


def test_worktree_remove_snapshots_target_worktree_config(callback_repo: Path) -> None:
    target = callback_repo.parent / "linked"
    subprocess.run(
        ["git", "-C", str(callback_repo), "worktree", "add", "--detach", str(target)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(callback_repo), "config", "extensions.worktreeConfig", "true"],
        check=True,
    )
    marker = _configure_callback(target, "clean")
    # Move the callback out of common config into the target-only configuration.
    subprocess.run(
        [
            "git",
            "-C",
            str(target),
            "config",
            "--worktree",
            "filter.example.clean",
            shlex.join([sys.executable, str(target / "callback.py")]),
        ],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(callback_repo), "config", "--unset", "filter.example.clean"], check=True
    )
    subprocess.run(
        ["git", "-C", str(callback_repo), "worktree", "remove", str(target)], capture_output=True
    )
    assert marker.exists(), "The target-only clean driver runs during unguarded remove"
    marker.unlink()
    git_commands.run_git(
        ["git", "worktree", "remove", str(target)], cwd=callback_repo, capture_output=True
    )
    assert not marker.exists()


def test_clone_snapshots_destination_conditional_filters(
    callback_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    included = callback_repo.parent / "clone-filters.gitconfig"
    marker = callback_repo.parent / "clone-filter-ran"
    callback = shlex.join(
        [
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).touch(); "
            "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
        ]
    )
    subprocess.run(
        ["git", "config", "--file", str(included), "filter.destination.smudge", callback],
        check=True,
    )
    global_config = callback_repo.parent / "global.gitconfig"
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    subprocess.run(
        ["git", "config", "--global", "includeIf.gitdir:**/cloned/.git.path", str(included)],
        check=True,
    )
    (callback_repo / ".gitattributes").write_text("tracked filter=destination\n")
    subprocess.run(["git", "-C", str(callback_repo), "add", ".gitattributes"], check=True)
    subprocess.run(
        ["git", "-C", str(callback_repo), "commit", "-m", "attributes"],
        check=True,
        capture_output=True,
    )
    target = callback_repo.parent / "cloned"
    raw = callback_repo.parent / "raw" / "cloned"
    subprocess.run(["git", "clone", str(callback_repo), str(raw)], check=True, capture_output=True)
    assert marker.exists(), "The pre-existing clone condition activates the smudge driver"
    marker.unlink()
    git_commands.run_git(
        ["git", "clone", str(callback_repo), str(target)],
        cwd=callback_repo.parent,
        check=True,
        capture_output=True,
    )
    assert (target / "tracked").read_text() == "initial\n"
    assert not marker.exists()


def test_snapshot_errors_keep_host_folder_facts_contract(callback_repo: Path) -> None:
    from omnigent.host.git_worktree import WorktreeError, list_worktrees, read_folder_facts

    (callback_repo / ".git" / "config").write_text("[broken\n")
    with pytest.raises(WorktreeError, match="configuration snapshot failed"):
        list_worktrees(repo_path=str(callback_repo))
    facts = read_folder_facts(str(callback_repo))
    assert facts.error and "configuration snapshot failed" in facts.error


def test_filtered_clone_materializes_checkout_with_explicit_guarded_fetch(
    callback_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subprocess.run(
        ["git", "-C", str(callback_repo), "config", "uploadpack.allowFilter", "true"], check=True
    )
    target = callback_repo.parent / "partial"
    real_run = subprocess.run
    fetch_policies = []

    def record_fetch(args, **kwargs):
        if args[1:2] == ["fetch"]:
            fetch_policies.append(kwargs["env"]["GIT_ALLOW_PROTOCOL"])
        return real_run(args, **kwargs)

    monkeypatch.setattr(git_commands.subprocess, "run", record_fetch)
    git_commands.run_git(
        [
            "git",
            "-c",
            "protocol.allow=never",
            "-c",
            "protocol.file.allow=always",
            "clone",
            "--filter=blob:none",
            callback_repo.as_uri(),
            str(target),
        ],
        cwd=callback_repo.parent,
        check=True,
        capture_output=True,
    )
    assert (target / "tracked").read_text() == "initial\n"
    config = subprocess.run(
        ["git", "-C", str(target), "config", "remote.origin.promisor"],
        check=True,
        capture_output=True,
    )
    assert config.stdout.strip() == b"true", "The fixture must be a real partial clone"
    assert fetch_policies == ["file"], "Checkout fetch retains caller protocol restrictions"


@pytest.mark.parametrize("new_branch", [False, True])
@pytest.mark.parametrize("failure", ["returncode", "exception", "timeout"])
def test_failed_destination_checkout_rolls_back_new_worktree(
    callback_repo: Path, monkeypatch: pytest.MonkeyPatch, new_branch: bool, failure: str
) -> None:
    target = callback_repo.parent / "failed-worktree"
    branch = "created" if new_branch else "existing"
    if not new_branch:
        subprocess.run(["git", "-C", str(callback_repo), "branch", branch], check=True)
    real_run = subprocess.run

    def fail_reset(args, **kwargs):
        if args[1:2] == ["reset"]:
            if failure == "exception":
                raise OSError("simulated checkout failure")
            if failure == "timeout":
                raise subprocess.TimeoutExpired(args, 0)
            return subprocess.CompletedProcess(args, 128, b"", b"simulated checkout failure")
        return real_run(args, **kwargs)

    monkeypatch.setattr(git_commands.subprocess, "run", fail_reset)
    args = ["git", "worktree", "add", *(["-b", branch] if new_branch else []), str(target)]
    if not new_branch:
        args.append(branch)
    if failure == "returncode":
        result = git_commands.run_git(args, cwd=callback_repo, capture_output=True)
        assert result.returncode == 128
    else:
        with pytest.raises((OSError, subprocess.TimeoutExpired)):
            git_commands.run_git(args, cwd=callback_repo, capture_output=True)
    assert not target.exists()
    registered = subprocess.run(
        ["git", "-C", str(callback_repo), "worktree", "list", "--porcelain"],
        check=True,
        capture_output=True,
    )
    assert str(target).encode() not in registered.stdout
    branch_result = subprocess.run(
        ["git", "-C", str(callback_repo), "show-ref", "--verify", f"refs/heads/{branch}"],
        capture_output=True,
    )
    assert (branch_result.returncode == 0) is (not new_branch)


@pytest.mark.parametrize("cleanup", ["remove", "branch"])
@pytest.mark.parametrize(
    "failure", ["text_result", "bytes_result", "checked", "exception", "timeout"]
)
def test_rollback_failure_preserves_original_checkout_failure(
    callback_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    cleanup: str,
    failure: str,
) -> None:
    from omnigent.host.git_worktree import WorktreeError, _run_git

    target = callback_repo.parent / "failed-cleanup"
    real_run = subprocess.run
    original_errors = []

    def fail_checkout_and_cleanup(args, **kwargs):
        if args[1:2] == ["reset"]:
            if failure == "exception":
                error = OSError("original checkout failure")
            elif failure == "timeout":
                error = subprocess.TimeoutExpired(args, 0)
            elif failure == "checked":
                error = subprocess.CalledProcessError(
                    128, args, output="", stderr="original checkout failure"
                )
            else:
                diagnostic = "original checkout failure"
                if failure == "bytes_result":
                    return subprocess.CompletedProcess(args, 128, b"", diagnostic.encode())
                return subprocess.CompletedProcess(args, 128, "", diagnostic)
            original_errors.append(error)
            raise error
        if (cleanup == "remove" and args[1:3] == ["worktree", "remove"]) or (
            cleanup == "branch" and args[1:3] == ["branch", "-D"]
        ):
            raise subprocess.CalledProcessError(73, args, stderr="cleanup-sensitive-detail")
        return real_run(args, **kwargs)

    monkeypatch.setattr(git_commands.subprocess, "run", fail_checkout_and_cleanup)
    args = ["worktree", "add", "-b", "new-branch", str(target)]
    detail = "Worktree rollback incomplete (Git exit 73)"
    if failure in {"exception", "timeout"}:
        with pytest.raises(WorktreeError) as raised:
            _run_git(args, cwd=str(callback_repo))
        assert raised.value.__cause__ is original_errors[0]
        assert detail in str(raised.value)
        assert ("timed out" if failure == "timeout" else "original checkout failure") in str(
            raised.value
        )
    elif failure == "checked":
        with pytest.raises(subprocess.CalledProcessError) as raised:
            git_commands.run_git(
                ["git", *args], cwd=callback_repo, check=True, capture_output=True, text=True
            )
        assert raised.value is original_errors[0]
        assert raised.value.returncode == 128
        assert "original checkout failure" in raised.value.stderr
        assert detail in raised.value.stderr
    else:
        result = git_commands.run_git(
            ["git", *args],
            cwd=callback_repo,
            check=False,
            capture_output=True,
            text=failure == "text_result",
        )
        assert result.returncode == 128
        diagnostic = result.stderr.decode() if isinstance(result.stderr, bytes) else result.stderr
        assert "original checkout failure" in diagnostic
        assert detail in diagnostic
        assert isinstance(result.stderr, str if failure == "text_result" else bytes)
    assert detail in caplog.text
    assert "cleanup-sensitive-detail" not in caplog.text
    assert target.exists() is (cleanup == "remove")
    branch = real_run(
        ["git", "-C", str(callback_repo), "show-ref", "--verify", "refs/heads/new-branch"],
        capture_output=True,
    )
    assert branch.returncode == 0, "Incomplete rollback must be reported, not called successful"


@pytest.mark.xfail(
    strict=True, reason="Accepted snapshot gap: a driver added after enumeration can execute"
)
def test_driver_added_after_snapshot_is_not_protected(
    callback_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = _configure_callback(callback_repo, "clean")
    original = git_commands._config_snapshot

    def insert_driver(*args, **kwargs):
        snapshot = original(*args, **kwargs)
        if not kwargs.get("trusted_helpers"):
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(callback_repo),
                    "config",
                    "filter.afterSnapshot.clean",
                    shlex.join([sys.executable, str(callback_repo / "callback.py")]),
                ],
                check=True,
            )
            (callback_repo / ".gitattributes").write_text("tracked filter=afterSnapshot\n")
        return snapshot

    monkeypatch.setattr(git_commands, "_config_snapshot", insert_driver)
    git_commands.run_git(
        ["git", "status", "--porcelain"], cwd=callback_repo, check=True, capture_output=True
    )
    assert not marker.exists(), "Expected failure: new driver executed in the accepted window"


def _unguarded_launches(source: str, *, github_wrapper: bool = False) -> list[int]:
    tree = ast.parse(source)
    findings: set[int] = set()
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }

    def scope_nodes(node):
        yield node
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                yield from scope_nodes(child)

    for scope in [
        tree,
        *(
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ),
    ]:
        nodes = list(scope_nodes(scope))
        tainted: set[str] = set()

        def is_git(node, tainted=tainted):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
            ):
                return node.value == "git" or bool(re.match(r"git(?:\.exe)?\s", node.value))
            if isinstance(node, ast.Name):
                return node.id in tainted
            if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
                first = node.elts[0]
                return is_git(first.value if isinstance(first, ast.Starred) else first)
            if isinstance(node, ast.BinOp):
                return is_git(node.left)
            if isinstance(node, ast.JoinedStr):
                return bool(node.values) and is_git(node.values[0])
            return False

        for _ in range(3):
            for node in nodes:
                if isinstance(node, ast.Assign) and is_git(node.value):
                    tainted.update(
                        target.id for target in node.targets if isinstance(target, ast.Name)
                    )
        for node in nodes:
            if isinstance(node, ast.Call):
                name = ast.unparse(node.func)
                executor = name.rsplit(".", 1)[-1] in {
                    "run",
                    "Popen",
                    "call",
                    "check_call",
                    "check_output",
                    "system",
                    "popen",
                    "execvp",
                    "execvpe",
                    "create_subprocess_exec",
                    "create_subprocess_shell",
                    "join",
                    "_run",
                }
                if executor and any(is_git(arg) for arg in node.args):
                    if not (github_wrapper and name == "_run"):
                        findings.add(node.lineno)
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if re.search(r"\$\(\s*git\s|subprocess\.run\(\s*\[\s*['\"]git['\"]", node.value):
                    findings.add(node.lineno)
    return sorted(findings)


def test_product_git_launches_cannot_bypass_guard() -> None:
    root = Path(__file__).parents[1] / "omnigent"
    findings = []
    for path in root.rglob("*.py"):
        if path == root / "git_commands.py":
            continue  # Bootstrap config reads and the one guarded executor live here.
        is_github = path == root / "runner" / "github_resource.py"
        source = path.read_text()
        if is_github:
            assert 'runner = run_git if argv[0] == "git" else subprocess.run' in source
        findings.extend(
            f"{path.relative_to(root)}:{line}"
            for line in _unguarded_launches(source, github_wrapper=is_github)
        )
    assert not findings, "Internal Git launch bypasses shared guard: " + ", ".join(findings)
    central = ast.parse((root / "git_commands.py").read_text())
    parents = {child: node for node in ast.walk(central) for child in ast.iter_child_nodes(node)}
    launches = []
    for node in ast.walk(central):
        if not isinstance(node, ast.Call) or ast.unparse(node.func) not in {
            "Popen",
            "subprocess.run",
            "os.execvpe",
        }:
            continue
        owner = node
        while owner in parents and not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
            owner = parents[owner]
        launches.append((getattr(owner, "name", "<module>"), ast.unparse(node.func)))
    assert sorted(launches) == sorted(
        [
            ("_config_snapshot", "Popen"),
            ("run_git", "subprocess.run"),
        ]
    ), "Unexpected execution path inside the shared Git guard"


@pytest.mark.parametrize(
    "source",
    [
        'subprocess.run(["git", "status"])',
        'argv = ["git", "status"]; subprocess.run(argv)',
        'self.run(sandbox, "git clone url target")',
        'script = "git_dir=$(git -C repo rev-parse HEAD)"',
        "wire = \"subprocess.run(['git','config'])\"",
    ],
)
def test_sweep_rejects_new_unguarded_launch(source: str) -> None:
    assert _unguarded_launches(source)
