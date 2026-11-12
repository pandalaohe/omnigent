# server

Hosts agents as a service. Accepts agent specs, stores them durably, and exposes APIs for managing and (eventually) invoking agents over HTTP.

The server is one way to deploy agents — the managed, multi-tenant, always-on way.

## Mount a board in another page

Use `/frame/task-board` or `/frame/usage/board` as an iframe URL. Usage account,
cost and issues paths also work beneath `/frame/usage/board/`. These routes use
the existing board settings and authentication, without the app sidebar or header.
Board navigation stays in the frame; links into the rest of Omnigent open a new
tab. Creating a session also opens the app. An unprovisioned server offers setup
in a new tab. An expired sign-in offers sign-in in a new tab and **Retry**, which
reloads the board after signing in.

To limit which pages can frame SPA documents, set the server YAML option:

```yaml
frame_ancestors:
  - "'self'"
  - https://panel.example.org:8443
```

The CLI reads this from `-c`, otherwise `OMNIGENT_CONFIG`, otherwise the resolved
default server config. An absent option or empty list adds no framing policy.
When configured, SPA documents (including login and approval pages) receive
`Content-Security-Policy: frame-ancestors ...`. Policy changes and removal
invalidate cached shell validators. This does not change API responses or the
separate, stricter CLI authentication pages and artifact policies.

Entries accept quoted CSP keywords `'self'` and `'none'` (the latter alone),
HTTP/HTTPS origins with optional ports, and HTTPS wildcard subdomains such as
`https://*.example.org`. The wildcard matches subdomains, not the bare domain.
CSP also allows an HTTP source to match its HTTPS upgrade. Paths, credentials,
queries, fragments, bare wildcards and malformed values refuse startup.

Every ancestor must be allowed: an allowed parent inside an unlisted grandparent
is refused. Include `'self'` for a same-origin page mount and any other parent
origins in a nested deployment. An iframe sandbox must permit scripts, same
origin, popups and popups escaping the sandbox for authentication links to work.

Session cookies retain `SameSite=Lax`. Signed-in boards require same-origin or
schemeful same-site hosting across the full ancestor chain (for example, HTTPS
subdomains of the same registrable domain, or different HTTPS ports). A panel
on another site or a LAN IP cannot use that session cookie; allowing its origin
does not fix authentication. Use the same-site hostname entry. CSP is a framing
policy, not authentication or isolation from trusted same-origin extensions.
No parent-origin CORS or WebSocket allowance is needed: board requests originate
from Omnigent itself; configured WebSocket origin checks still need its origin.
