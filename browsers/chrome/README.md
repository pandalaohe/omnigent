# Chrome page evidence companion

This MV3 development slice captures an element, region or selected visible text
into a temporary extension-owned preview. It does not pair, save a tray, upload
or send a message. Closing/restarting its worker can clear the preview.

With the workspace's Node and pnpm installed, run from the repository root:

```sh
pnpm install --frozen-lockfile --filter omnigent-chrome
pnpm --filter omnigent-chrome run build
pnpm --filter omnigent-chrome run type-check
pnpm --filter omnigent-chrome test
```

The cross-platform Node/esbuild build writes `browsers/chrome/dist`. Later release
acceptance loads that directory unpacked in Chrome 116 or newer on macOS and
Windows. `manifest.json` contains a public identity key, not a signing secret;
the corresponding private key was discarded. Keep this public key unchanged so
the extension ID and later pairing origin stay stable. The manifest test pins the
derived ID. Shortcut: Cmd+Shift+Y on macOS, Ctrl+Shift+Y on Windows (customizable
in Chrome's extension shortcuts).

Invoke the Chrome toolbar action or shortcut on an ordinary HTTP(S) page. The
side panel opens before asynchronous work, then the top-frame isolated picker
starts after panel resizing. Choose Element, drag a Region, or choose Text, select
page text and click Capture selected text. Review the visible crop and quoted page evidence in
the panel. Escape closes the picker. A screenshot error is visible and requires
another explicit capture; it never silently becomes an image-free annotation.

Internal pages, the Web Store, PDF viewers and file URLs are unsupported. Open
shadow trees have no stable CSS selector; frame/closed-shadow internals can only
be represented as visible regions. Shadow styling does not protect against a
hostile page modifying its DOM. No page messages, externally connectable API,
registered content scripts, React-fiber inspection or form values are used.
Page URLs omit credentials, query and fragment. Crops and selected visible text
can still contain private material: inspect them before use.

Automated checks cover protocol limits, desktop formatter parity, DOM evidence,
sender isolation, user-gesture opening, capture races, crop rounding and manifest
identity. Real Chrome layout/permission behavior, Retina and Windows display
scaling remain release acceptance checks; mocked tests do not establish them.
