# Chrome page evidence companion

An MV3 companion for reviewing a page, keeping local annotation drafts and sending
an ordered tray as one user message with images to a chosen Omnigent session.
Chrome 116 or newer is required. The server must enable annotation device grants
in accounts mode and serve HTTPS with a valid certificate.

With Node 24 and pnpm installed, run from the repository root:

```sh
node --version
pnpm install --frozen-lockfile --filter omnigent-chrome
pnpm --filter omnigent-chrome run build
```

The version must be `v24.x`. The cross-platform Node/esbuild build writes
`browsers/chrome/dist`; `pnpm --filter omnigent-chrome run check` runs the automated
checks and rebuilds it. Build separately in each platform's own checkout.

- **macOS:** `open browsers/chrome/dist` opens the built folder in Finder. In Chrome,
  open `chrome://extensions`, enable **Developer mode**, choose **Load unpacked**,
  and select that `browsers/chrome/dist` folder in the Finder dialog.
- **Windows:** run the same build commands in **PowerShell 7** from the repository
  root, then `explorer.exe (Resolve-Path .\browsers\chrome\dist).Path`. In Chrome,
  open `chrome://extensions`, enable **Developer mode**, choose **Load unpacked**,
  and select that local `browsers\chrome\dist` folder in Explorer.

Its fixed public manifest key derives ID `kijpmhjibehfipeillfolgdodncokjol`;
the private key was discarded. Preserve the public key across builds. The server's
configuration must include `OMNIGENT_DEVICE_GRANT_ENABLED=1`,
`OMNIGENT_CHROME_ANNOTATIONS_ENABLED=1` and
`OMNIGENT_CHROME_EXTENSION_ORIGIN=chrome-extension://kijpmhjibehfipeillfolgdodncokjol`.
Use an accounts-mode server with a valid HTTPS certificate and annotation storage
available; the extension does not configure or release the server. Verify the ID
on each browser's extension page, pin its toolbar action, and pair each browser
separately so it has its own grant. After rebuilding, use **Reload** on that page:
Reload and Chrome restarts preserve saved drafts. **Remove / Uninstall** deletes
the extension's local data; do not use it to test persistence.

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
“Include query and fragment in future captures” choice is scoped to the current
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

## Manual acceptance after release

Run this independently on macOS and Windows after a server build with this feature
is released. Use Chrome 116+ and the **web UI only**, with the desktop app closed.
Keep the normal server/host running. Have two editable, available sessions, and
follow the build, load and pairing steps above. Record each case as **PASS**, **FAIL**
or **PENDING**; one platform's result does not prove the other. Node/jsdom checks
are not evidence of physical capture or display scaling.

Serve the maintained [known-pixel page](manual/capture.html) from the repository
root with the project's existing uv/Python environment. This is a foreground
stdlib server, bound only to loopback; stop it with Ctrl+C when finished.

macOS:

```sh
uv run --no-sync python -m http.server 8765 --bind 127.0.0.1 --directory browsers/chrome/manual
```

Windows, in PowerShell 7:

```powershell
uv run --no-sync python -m http.server 8765 --bind 127.0.0.1 --directory .\browsers\chrome\manual
```

Open `http://127.0.0.1:8765/capture.html?demo=public#sample` in Chrome. This HTTP
fixture is page evidence; the chosen Omnigent server must still use HTTPS. The
query and fragment are public dummy values for the optional full-URL check. The
page has no extension bridge, credentials or external assets.

| Case                          | Actions and expected observation                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| A1 · Pair and target          | Follow steps 1–3 above, compare the public code/browser label on the signed-in page and approve. In the web UI, open session A and select **Receive web annotations**; it becomes **Receiving web annotations**. Use **Refresh sessions** in the panel: A is pinned as **Receiving in Omnigent**. Explicitly choose another editable session B and verify its title/host before capture.                                                                                                                                                                                                                                                                                                   |
| A2 · Two notes, one message   | On the fixture, invoke the toolbar/shortcut and capture the teal target with **Element**. Save note `Check the teal border.` Then use **Text**, select `Total: "€25"` and click **Capture selected text**; save `Keep this text with its context.` Inspect quote/context, URL and crops. Use **Move up / Move down** to restore element then text order. Press **Send 2 annotations to …** once. In the actual session B's web UI, observe one ordinary user message containing both notes and images in that order. The receipt's **Open the actual session** must open B under the chosen server/basepath at `/c/<session_id>`. Acceptance is not proof that the agent read the message. |
| A3 · Region and top layer     | Make separate unsent captures with **Region** across the opaque iframe, and with the fixture's **Open dialog** active. Frame internals are region evidence, not invented element details. The picker remains visible/usable over the modal. Inspect each preview; delete these unsent items when finished unless deliberately sending another batch.                                                                                                                                                                                                                                                                                                                                       |
| A4 · Pixel bounds             | With the panel settled, capture the public fixture's fixed target, a dragged region and a partly clipped target. Deliberately Send each test batch to the explicitly chosen receiver. Open its actual received ordinary message; match capture ID/order to the image and **Page evidence (untrusted JSON)** geometry. Use the method below, repeating after panel resize and available display/page-zoom changes. Unknown/missing receipt or no actual message leaves A4 PENDING; do not Send again automatically. Unsupported pinch zoom must request a reset.                                                                                                                            |
| A5 · Local edits and URL      | Edit **What should change?**, use **Save note**, **Remove screenshot**, and **Recapture** on unsent evidence. A failed capture requires explicit **Send without screenshot** or recapture. With the full-URL checkbox off, query/fragment are absent. Enable **Include query and fragment in future captures**, recapture this public fixture, inspect the dummy values, then use **Remove query and fragment**. **Copy notes** includes text/evidence and states that screenshots are absent. No action here sends automatically.                                                                                                                                                         |
| A6 · Keyboard and persistence | Test the toolbar action and macOS Cmd+Shift+Y / Windows Ctrl+Shift+Y; resolve conflicts at `chrome://extensions/shortcuts`. Cmd/Ctrl+Enter saves a note, Shift+Enter inserts a newline, Escape exits picking without losing the tray. At a narrow panel width, Tab reaches labelled controls with visible focus. Save an unsent draft, use extension **Reload**, then restart Chrome: the saved tray returns and no Send occurs.                                                                                                                                                                                                                                                           |
| A7 · Receiver changes         | If receiving changes during ordinary use, the panel offers **Receiving session changed — use …**. Existing drafts keep their original target until an explicit switch. An unavailable receiver stays visibly selected; nothing silently retargets or sends.                                                                                                                                                                                                                                                                                                                                                                                                                                |
| A8 · Failure and recovery     | When an offline/archived target, revoke/expiry, response loss or interrupted worker is encountered in normal use, record the visible error or **Delivery Unknown** and retained draft. Do not stop a host, induce a network outage or discard a response just to fill this row; an unobserved case is PENDING. **Check delivery** is read-only and adds zero messages. A live-grant absent receipt is provisional. When offered, **Retry same batch and target** preserves its public batch/capture IDs and target; a known ready receipt may instead offer **Retry first dispatch**. Neither action silently creates a second message.                                                    |
| A9 · Old grant check          | If an old connection needs recovery, use **Copy public IDs** and **Open Omnigent delivery check**. In `/settings/account#connected-apps-heading`, **Check an old annotation batch** shows those exact IDs and the actual target when recorded. **Delivery unknown** and **Still unconfirmed** do not authorize migration; **Delivery paused** is not confirmed delivery. Only a closed grant's exact **Not admitted** result can support the panel's explicit **I checked: not admitted — move draft** confirmation. The web page does not send a trusted result to the extension or resend a batch. A new grant's token cannot read an old grant's receipt.                               |

For A4, the teal target's **outer border box is 320 × 160 CSS pixels**, including a
4 CSS-pixel `#0b6b63` border and `#fff7e6` fill. The live readout reports its current
viewport-relative `left/top/right/bottom`, `innerWidth/innerHeight`, `clientWidth`,
scroll and visual viewport. Opening the panel changes these metrics; use the
capture's matching geometry, not values taken before resizing or scrolling.

After deliberately sending the public fixture batch, open the actual received
ordinary message in the explicitly chosen receiver. For each item, read the exact
`geometry.rect`, `geometry.viewport` and `geometry.bitmap_scale` from its
**Page evidence (untrusted JSON)** block. Match its capture ID and item order to
the associated image immediately after that evidence. The extension's evidence
details do not expose this geometry. An absent/unknown receipt, missing actual
message or missing measured geometry leaves A4 PENDING; do not Send again
automatically.

Use that message's measured `bitmap_scale.x/y`: `sx = W / Vw`, `sy = H / Vh`.
If full bitmap dimensions are not shown, record them as derived from those
measured ratios and the captured viewport, or mark them unavailable. Do not
derive a substitute ratio from crop dimensions, DPR, OS scaling or browser chrome
offsets. Clip the CSS box to the viewport, then compare the expected bitmap bounds
`floor(left*sx), floor(top*sy), ceil(right*sx), ceil(bottom*sy)` with the observed
crop and PNG dimensions, clipped to `[0,W] × [0,H]`. Outward rounding can add less
than one physical pixel per fractional edge; additional edge misalignment must be
within one physical pixel. A scaled-to-fit preview alone cannot establish that.
Save only the matched public fixture crop from that actual message and inspect
its PNG at 1:1 and its image dimensions; never retain a full-tab screenshot for this
procedure. On macOS use the actual Retina ratio; on Windows use actual display
scaling, preferably 125% and/or 150% when available. Record unavailable settings
as PENDING rather than changing the result or inferring it from another run.

Return one copy of this template **per platform**, with no credentials or private
page evidence. Attach only the public fixture's relevant crop/receipt observations.
Keep unrun scenarios PENDING and explain each FAIL or PENDING.

```text
Platform / OS version:
Source HEAD (git rev-parse HEAD):
Chrome version / Node version:
Extension ID (expected kijpmhjibehfipeillfolgdodncokjol):
Own unpacked build and separate browser grant: yes / no
Web UI only; desktop app closed: yes / no
OS display scaling / display used:
Chrome page zoom / visualViewport.scale and offsets:
A4 explicitly chosen receiver / receipt batch ID / actual message:
A4 per-item capture ID / order / matching saved crop filename:
Per-item exact geometry.rect / geometry.viewport / geometry.bitmap_scale (actual message JSON):
CSS viewport Vw×Vh / document clientWidth / scroll x,y:
CSS target bounds left,top,right,bottom:
Measured bitmap_scale x,y:
Full bitmap W×H: observed / derived from measured scale / unavailable
Expected bitmap bounds L,T,R,B / expected crop width×height:
Observed PNG width×height / per-edge alignment error:
Second panel width / zoom / monitor observation, or PENDING reason:
A1 pair / pinned A / explicit B: PASS | FAIL | PENDING — evidence
A2 two notes / one actual message / ordered images / actual B link: PASS | FAIL | PENDING — evidence
A3 iframe region / modal dialog: PASS | FAIL | PENDING — evidence
A4 Retina or Windows scaling / clipped target / edge rounding: PASS | FAIL | PENDING — evidence
A5 edit / remove image / recapture / full URL / text-only Copy: PASS | FAIL | PENDING — evidence
A6 platform shortcuts / narrow keyboard / Reload / Chrome restart: PASS | FAIL | PENDING — evidence
A7 receiver change / no automatic retarget: PASS | FAIL | PENDING — evidence
A8 observed failures / Unknown / read-only Check / explicit same-key Retry: PASS | FAIL | PENDING — evidence
A9 old-grant signed-in check / explicit confirmation only: PASS | FAIL | PENDING — evidence
Unrun subcases, exact failure observations and public evidence filenames:
```
