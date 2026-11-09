# Browser extensions

This desktop feature accepts user-supplied Chrome Manifest V3 packages. There is
no extension store, automatic download or update check. Electron supports only a
subset of Chrome APIs; an accepted package is not a compatibility guarantee.

## Get a package

Get an unpacked folder or a `.zip` directly from the extension publisher's
release page. For Bitwarden, use the `dist-chrome` archive from the
[official client releases](https://github.com/bitwarden/clients/releases).
The selected folder or archive must contain `manifest.json` at its root. A
Chrome Web Store URL or `.crx` is not an accepted package. Do not obtain password
manager packages from an unknown mirror.

## macOS and Windows

1. In the desktop app, open Settings → General. Turn on **Keep in-app browser
   login**, then close and reopen Browser so its persistent session is used.
2. Under **Browser extensions**, choose **Add extension**, then **Zip package**
   or **Unpacked folder**. Select the publisher's package in the native picker.
3. Read the native confirmation. It names the extension, version, permissions
   and host access. Extensions can read Browser pages, including pages an agent
   uses. Manifest V2 and required `nativeMessaging` / `debugger` permissions
   are refused in main. Optional declarations are listed as declared but not
   granted; requests for those capabilities resolve false.
4. Choose **Install extension** to load it. The app copies the package; the
   source folder or zip can then be removed.
5. Open a page in Browser. The puzzle button opens the installed extensions'
   popups. Extensions without a popup operate through their content scripts.
6. Use the switch beside an extension to disable or enable it. Browser pages
   reload after extension changes. **Remove** asks
   in a native dialog, then removes the copied package and local extension data
   across this desktop's saved Browser sessions. Cloud data is unaffected.

There is no automatic update. To change versions, remove the old package and add
the new one; removing clears local state, so be ready to sign in again. Windows
native dialogs and runtime behavior require verification on Windows.

## Let agents fill through Bitwarden

When the manifest names Bitwarden, the install confirmation offers a separate,
default-off **Allow agents to fill saved logins through this extension** choice.
The name does not authenticate the publisher. A package you knowingly approve
is trusted, matching the access installing it already grants. Leave this off
when you do not trust the package. Your own popup use still works.

Consent is bound to a SHA-256 digest of the complete installed package. Changed
content clears consent; changing the bytes back does not restore it. Use
**Review permissions** to review the current package and grant consent again.
Changes to manifest permissions also require a new native confirmation before
loading. Duplicate runtime extension IDs are rejected.

Agent fill still requires the separate per-site approval. Revoke those approvals
under **Approved sites**. The session receives success or failure, never the
saved password. Unlock, select and fill manually in the extension popup when
agent fill is unavailable. New accounts may require Bitwarden's manual Add;
automatic save prompts and inline account-row filling remain unverified.

Removal uses an app-owned storage-only helper with the installed runtime
identity. Disabling or corrupting a package cannot make removal execute that
package. This current removal mechanism is verified at unit level only,
including dormant partitions. Windows is unverified.

## Verification recipe

Use a dedicated throwaway desktop profile and fake vault. Never inspect a real
vault or capture its popup. Verify import and native cancellation, MV2 and
forbidden permissions, unsafe zip paths, two extensions at once, enable/disable,
remove and local storage isolation. Check default-off consent, changed-content
revocation, duplicate IDs, and the existing per-site fill approval. Record
platform, revision, command and exit status. Remove only the test profile and
processes created by the test.

## Tried, not guaranteed

- Bitwarden `dist-chrome` 2026.10.2: previous macOS trial established login,
  unlock and popup fill. This task's new import/login/unlock/popup-fill checks
  passed using a fake vault. An after-unlock agent-fill probe returned no
  available login. A confirmed reload-state defect has a unit regression and
  correction; its relationship to that native result is unproven, and the
  corrected native agent path has not been rerun.
- Synthetic MV3 storage extension: loaded beside Bitwarden on macOS; native
  optional requests denied, native-messaging main handlers refused, and debugger
  API absent.

A successful test of one version does not guarantee later versions or other
Chrome extensions. Known unsupported APIs include `chrome.privacy`.
