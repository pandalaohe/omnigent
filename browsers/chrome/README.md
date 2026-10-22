# Chrome page evidence companion

An MV3 companion for reviewing a page, keeping local annotation drafts and sending
an ordered tray as one user message with images to a chosen Omnigent session.
Chrome 116 or newer is required. The server must enable annotation device grants
in accounts mode and serve HTTPS with a valid certificate.

With Node 24 and pnpm installed, run from the repository root:

```sh
pnpm install --frozen-lockfile --filter omnigent-chrome
pnpm --filter omnigent-chrome run check
```

The cross-platform Node/esbuild build writes `browsers/chrome/dist`. Load that
folder unpacked from Chrome's extension management page on macOS or Windows.
Its fixed public manifest key derives ID `kijpmhjibehfipeillfolgdodncokjol`;
the private key was discarded. Preserve the public key across builds. The server's
annotation-origin configuration must allow this exact extension origin.

1. Open the side panel using the toolbar action or Cmd+Shift+Y on macOS /
   Ctrl+Shift+Y on Windows. A shortcut conflict can be resolved in Chrome's
   extension shortcuts. Only the toolbar action/shortcut grants activeTab access.
2. Enter the explicit HTTPS server address (including any deployment basepath)
   and a browser label. Connect requests optional access to that chosen origin
   directly in the panel gesture. Sign in again on the opened first-party
   `/oauth/device?user_code=…` page, compare the code and approve. Other auth modes
   or disabled annotation pairing require a server configuration change.
3. Review the pinned “Receiving in Omnigent” session, or explicitly choose another
   target. Rows show title, short ID, host, workspace and availability. Search and
   pagination request session rows only. An offline receiver stays selected and
   unavailable. A changed receiving session offers an explicit switch; existing
   drafts keep their saved target.
4. Invoke the toolbar/shortcut on an ordinary HTTP(S) page. Choose Element,
   drag a Region, or choose Text, select visible text and click Capture selected
   text. Escape closes the picker. The panel shows the crop and bounded page
   evidence; add a note and save it with the button or Cmd/Ctrl+Enter.
5. Inspect each URL/crop/note, then press the button naming the annotation count,
   target title and host. This freezes the ordered batch before uploads. New
   captures enter a separate unsent tray. An accepted receipt means durable
   admission to that session; it does not mean an agent has consumed it.

Drafts and cropped PNG Blobs live in native IndexedDB, partitioned by canonical
server/basepath, verified account and grant. They survive panel/worker restarts
without an unsent TTL or automatic eviction. Limits are 10 retained items, 20 MiB
of crop data, 2 MiB per crop and 4000 characters per note. A note must be nonempty
before Send. “Saved locally” appears after the transaction commits. On a storage
error the current input/temporary preview remains visible; free space or copy the
text, then explicitly save again before closing the panel.

Edit, reorder, delete, remove an image or explicitly recapture an unsent item.
A recapture keeps the saved original until the replacement commits. Capture
failure requires an explicit “Send without screenshot” choice or recapture;
it never silently drops an image. Removing a successfully measured image keeps
its measured geometry. A failed capture has unknown bitmap scale, never a
fabricated DPR-derived scale.

Default page URLs omit credentials, query and fragment. The panel's explicit
“Include query and fragment for future captures” choice is scoped to the current
connection. Its full URL comes from Chrome's sender metadata and is shown in the
preview; it can be removed from an item before Send. Credentials are always
stripped. Visible selected text and crops may contain private material, so inspect
them. Copy produces note/evidence text only and states that screenshots are absent.

If an upload or request fails, nothing is sent again automatically. A lost submit
acknowledgement or interrupted worker becomes **Delivery Unknown**. Check delivery
is a read-only POST lookup. A live-grant “not recorded” result remains provisional;
an explicit Retry keeps the original batch, target, order and capture IDs. Expired
staging reuploads the same saved crop bytes/hash only after that action. A known
ready receipt may offer explicit first-dispatch Retry using the complete original
body and IDs; it does not upload or make a second message. Unconfirmed receipts
retain that body; confirmed receipts keep metadata for 30 days. Session links are
constructed from the chosen server/basepath, never a response-provided URL.

Disconnect offers revoke-and-keep, explicitly local-only keep, or revoke-and-delete
for that grant. Local-only disconnect leaves revocation unconfirmed. No option
silently deletes unsent notes. After reconnecting with the same server/account,
previous-grant trays appear read-only. Never-attempted drafts can be explicitly
moved with fresh capture IDs. Attempted batches require closing the old grant and
checking the exact public grant/batch IDs in the signed-in first-party
`/settings/account#connected-apps-heading` view. Only an explicit exact
not-admitted confirmation enables migration; Unknown does not. The page sends no
trusted result back to the extension. A possible-duplicate copy is a separate
confirmed action with fresh IDs; it is disabled when local evidence has already
been removed. Old receipts are never queried with a new grant's token, and data
cannot migrate across accounts.

Internal pages, the Web Store, PDF viewers and file URLs are unsupported. Frame
and closed-shadow internals are visible-region evidence only. Shadow styling is
not a security boundary. No page messages, externally-connectable API, all-frame
permission, history, cookies, form values or React internals are used. Credentials
remain worker-owned in trusted-only local storage; panel messages contain public
JSON-safe metadata and cropped previews. Network requests omit cookies, reject
redirects and time out; no credential is included in a URL, panel or log.

Automated Node/jsdom checks cover real worker message handlers, the IndexedDB
owner, auth rotation/lost acknowledgements, persistent batch/recovery contracts,
JSON messaging, DOM text rendering, capture races, crop math and stable build
assets. jsdom callback/popover stubs represent API boundaries only. They do not
establish trusted physical input, Chrome top-layer rendering, optional-permission
behavior, Retina/Windows scaling or actual capture. Release acceptance must check
pairing, two-note Send, Unknown/Check/explicit Retry, worker restart, receiver
changes, an iframe region and display scaling on the user's macOS/Windows Chrome.
No personal browser profile or real external upload was used for automated proof.
