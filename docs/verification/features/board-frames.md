# Board frames

Use the [verification entry](../README.md) for test isolation and cleanup, and
the [server configuration reference](../../../omnigent/server/README.md#mount-a-board-in-another-page)
for URLs, hosting conditions and framing policy syntax.

## Automated checks

Run `tests/server/test_frame_ancestors.py` and
`tests/server/integration/test_base_path.py` through the isolated pytest wrapper.
The first covers validation, CLI config delivery, document headers and cache
transitions; the second preserves base-path handling.

With Node 24, run web vitest for `App.test.tsx`, `lib/identity.test.ts`,
`lib/boardFrame.test.tsx`, `pages/taskBoard/TaskBoardActions.test.tsx` and
`pages/usageBoard/usageBoardReview.test.tsx`.

After building the SPA, run `tests/browser_ui/test_board_frames.py` through the
isolated wrapper with `--browser-ui-skip-build`. It serves static pages on local
HTTPS origins, with browser-contract API fixtures: same-site cookies, nested
ancestors, CSP refusal and the cross-site sign-in panel. It does not establish
live accounts or OIDC provider integration.

## Deployment acceptance

On a provisioned, signed-in instance, allow a same-site HTTPS panel origin plus
`'self'`. Frame both board URLs in that panel. Both should show their boards
without the app sidebar or header. Follow an account tile and its settings/back
links: they should stay below `/frame/`. Session and app-settings links should
open new tabs. Expire the session: sign-in should open a new tab, and **Retry**
should reload the existing frame after sign-in. Check initial signed-out entry
too. Repeat with OIDC when that deployment exists.

From an unlisted panel origin, the browser should refuse the board document.
Repeat with an unlisted outer page containing a listed parent. Record the exact
build, auth mode, ancestor URLs and console refusal. Restore the deployment's
previous policy after the trial. Production deployment is a separate release step.
