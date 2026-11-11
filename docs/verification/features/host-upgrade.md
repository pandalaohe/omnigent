# Host upgrade verification

Use a disposable uv tool install and isolated application data for automated trials.
Record the source revision, OS, Python and uv versions with the result.

The related component checks are `tests/cli/test_update_check.py`,
`tests/cli/test_upgrade_command.py`, `tests/host/test_windows_upgrade.py`,
`tests/host/test_windows_task.py`, `tests/host/test_service.py`,
`tests/host/test_restart.py`, `tests/test_install_ledger.py` and
`tests/test_uninstall_cli.py`.
`tests/cli/test_cli.py` covers the CLI aliases. Use the repository's test isolation
entry point. Synthetic process tables prove selection and recovery branches; they
do not establish native Windows file-sharing behavior.

For a native Windows trial, install the candidate into a disposable UV_TOOL_DIR and
UV_TOOL_BIN_DIR. Keep a second process holding a temporary launcher copy open without
FILE_SHARE_DELETE. Exercise `retained_install` on that disposable layout: a lock
released within three seconds must permit the rename; a sustained lock must report
the applications returned by Restart Manager, skip the installer and restore any
already-renamed launchers. Check that `file_users` reports the known holder PID.
If Restart Manager is unavailable, expect the error to include its failure and
visible process candidates with PID and image. These candidates are not confirmed
lock holders; denied process visibility must be reported explicitly.
Start the upgrade through the installed `omni.exe` too: uv's launcher puts that file on
`sys.path`, so a metadata lookup holds it open until `retained_install` releases it.
After a successful install the old launcher and environment become `<name>.trash-<hex>`
siblings; the next upgrade removes them, while `.previous-*` and `.failed-*` copies stay.
Stop only the trial's processes and remove only its temporary directories.

Windows autostart uses `omnigent host enable` and a per-user Task Scheduler task.
The task runs the tool environment's `pythonw.exe`, loads the allowlisted
enable-time environment from an owner-only file, and supervises a foreground Host.
Task Scheduler ending its action does not prove its children exited: stop disables
and verifies the task, suspends relaunch supervisors, captures descendants, ends
the action and checks the captured process identities have exited.
On a temporary action that exits 1, the tested scheduler did not restart it;
the Windows service entry therefore relaunches nonzero workers with a five-second
backoff. Exit 0 or permanent failure 78 stops the loop. Remove that loop when
upstream supplies the Windows service or scheduler nonzero-exit restart is proved.

After installing the candidate on a Windows host, the operator verifies:

1. From a terminal with the intended PATH, run `omnigent host enable` for local
   mode, or `omnigent host enable --server <url>` for a remote server.
2. Check `omnigent host status`, sign out and back in, then check status again.
   The Host returns without a console window. Quiesce any previous legacy
   autostart task through the operator's migration procedure first.
3. Preview `omnigent uninstall`: expect a task/env-file cleanup preview plus the
   manual `uv tool uninstall omnigent` step. A preview leaves the service intact.
   Test `omnigent host disable` on a disposable installation: the task, environment
   file and ledger entry disappear and no captured Host processes survive.

The final installed-host acceptance belongs to the operator, from a terminal outside
an Omnigent session, after publishing the approved candidate:

1. Record `omni --version` and the intended channel head.
2. Run `omni up`. Expect running turns to drain, supervisor shutdown, installer
   progress and supervisor restart. `--force` skips the running-turn wait.
   Windows upgrades also work without an installed service and require no
   PowerShell or custom wrapper. A failed install restores and restarts the
   existing service. A failed disable or surviving captured process blocks install.
3. Run `omni --version`. Its commit must match the intended head; confirm the Host
   reconnects and existing session history remains accessible.
4. On failure, preserve the command output, reported PID/application and resulting
   CLI version. Do not remove retained installation backups while processes use them.

An installed-host upgrade and Restart Manager's real output remain unverified until
those native checks have been performed against the candidate.
