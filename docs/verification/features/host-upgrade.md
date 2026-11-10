# Host upgrade verification

Use a disposable uv tool install and isolated application data for automated trials.
Record the source revision, OS, Python and uv versions with the result.

The related component checks are `tests/cli/test_update_check.py`,
`tests/cli/test_upgrade_command.py`, and `tests/host/test_windows_upgrade.py`.
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

The final installed-host acceptance belongs to the operator, from a terminal outside
an Omnigent session, after publishing the approved candidate:

1. Record `omni --version` and the intended channel head.
2. Run `omni up --force`. Expect supervisor shutdown, installer progress and supervisor
   restart. An install pinned by the old custom updater with matching channel config
   and rollback state should announce restored branch tracking once.
3. Run `omni --version`. Its commit must match the intended head; confirm the Host
   reconnects and existing session history remains accessible.
4. On failure, preserve the command output, reported PID/application and resulting
   CLI version. Do not remove retained installation backups while processes use them.

An installed-host upgrade and Restart Manager's real output remain unverified until
those native checks have been performed against the candidate.
