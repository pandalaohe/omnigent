# Extension manifest

An extension registers a lightweight factory under the
`omnigent.extensions` Python entry-point group:

```toml
[project.entry-points."omnigent.extensions"]
review = "acme_review.plugin:get_manifest"
```

The factory returns immutable values from `omnigent.extensions` and should not
import runtime or UI implementation modules.

```python
from omnigent.extensions import (
    EXTENSION_API_VERSION,
    ExtensionEntrypoints,
    ExtensionManifest,
    ExtensionPermission,
    PageContribution,
    PrimaryNavigationContribution,
)


def get_manifest() -> ExtensionManifest:
    return ExtensionManifest(
        id="acme.review",
        display_name="Acme Review",
        distribution="omnigent-acme-review",
        version="1.0.0",
        requires_omnigent=">=0.11,<1",
        extension_api=EXTENSION_API_VERSION,
        entrypoints=ExtensionEntrypoints(
            browser="dist/extension.js",
            browser_css="dist/extension.css",
        ),
        permissions=frozenset({ExtensionPermission.NAVIGATION}),
        pages=(
            PageContribution(
                id="acme.review.dashboard",
                title="Reviews",
                route="reviews",
                view="dashboard",
            ),
        ),
        primary_navigation=(
            PrimaryNavigationContribution(
                id="acme.review.primary-nav",
                label="Reviews",
                page="acme.review.dashboard",
                icon="search",
                order=500,
            ),
        ),
    )
```

## Rules

- IDs are lowercase, publisher-qualified, immutable, and globally collision
  checked. Contribution IDs begin with the owning extension ID.
- Page routes contain exactly one safe segment and are always namespaced.
  IDs, route uniqueness, navigation references, and activation metadata apply
  equally to view and external pages.
- The manifest distribution/version must match installed package metadata.
- `requires_omnigent` is a PEP 440 release-line range.
- Browser paths are fixed to `dist/extension.js` and optional
  `dist/extension.css` inside the entry point's verified import package.
- Built-ins are reserved. All community extensions participating in a collision
  are disabled deterministically.
- One invalid field rejects the whole extension; one rejected extension does not
  prevent others from loading.

## External pages

`ExtensionManifest.pages` accepts both `PageContribution(id, title, route, view)`
and `ExternalPageContribution(id, title, route, url)`. External pages require
`ExtensionPermission.PAGES_EXTERNAL` (`"pages.external"`). Only view pages
require a browser entrypoint; an external-only extension can ship no bundle.

```python
from omnigent.extensions import (
    EXTENSION_API_VERSION,
    ExtensionManifest,
    ExtensionPermission,
    ExternalPageContribution,
)


def get_manifest() -> ExtensionManifest:
    return ExtensionManifest(
        id="acme.dashboard",
        display_name="Dashboard",
        distribution="omnigent-acme-dashboard",
        version="1.0.0",
        requires_omnigent=">=0.11,<1",
        extension_api=EXTENSION_API_VERSION,
        permissions=frozenset({ExtensionPermission.PAGES_EXTERNAL}),
        pages=(
            ExternalPageContribution(
                id="acme.dashboard.overview",
                title="Dashboard",
                route="overview",
                url="https://dashboard.example.com/",
            ),
        ),
    )
```

The URL must be an absolute HTTP or HTTPS string of at most 2048 characters,
without whitespace, control characters, or userinfo. It must have
a valid port when specified and an ASCII DNS, IPv4, or bracketed IPv6 host.
DNS labels use letters, digits, and hyphens, with no leading or trailing hyphen;
a trailing dot is allowed. Percent-encoded hosts and IPvFuture are rejected.

The catalog preserves `id`, `title`, `route`, and `view` for view pages and adds
`kind` and `url`:

| Page kind | `kind` | `view` | `url` |
| --- | --- | --- | --- |
| Browser view | `"view"` | View symbol | `null` |
| External page | `"external"` | `null` | Declared URL |
| Server-proxied page | `"proxied"` | `null` | Omnigent proxy path |

Web consumers treat missing `kind` as `"view"` for older catalogs. Unknown kinds
show the not-found page. See the [trust model](extensions.md#trust-model) for
framing restrictions and navigation limitations.

### Server-proxied pages

Set `ExternalPageContribution.proxy=ExternalPageProxy(...)` and grant both
`pages.external` and `pages.proxy`. The URL selects a trusted HTTP(S) upstream
root; an initial query or fragment is preserved in the catalog URL.

```python
ExternalPageProxy(
    allowed_routes=(("GET", "/"), ("GET", "/api/summary.json"), ("POST", "/api/settings")),
    request_headers=(("x-dashboard", "1"),),
)
```

The catalog returns `/v1/extensions/{extension_id}/proxy/{route}/`. The server
strips this prefix and preserves the query. Each request needs normal Omnigent
authentication and an exact declared method/path pair; `GET /` must be declared.
Paths are canonical ASCII absolute paths without traversal or percent encoding.
Writes require a matching browser Origin and, when present, `Sec-Fetch-Site: same-origin`.

The proxy forwards Content-Type and Accept, sets upstream Host and
X-Forwarded-Prefix/Host/Proto, and rewrites write Origin to the upstream origin.
`request_headers` adds static operator-declared headers; it never copies arbitrary
browser headers. Browser Cookie, Authorization, and identity headers are discarded.
Upstream Set-Cookie and redirect destinations are discarded; redirects return 502.
Content-Type, Content-Language, CSP, and X-Frame-Options are retained. Responses use
`Cache-Control: no-store`; upstream errors preserve their status, transport failures
return 502, and timeouts return 504. Requests are limited to 1 MiB and decoded
responses to 8 MiB. Upstreams must generate prefix-relative links themselves.

## Compatibility

The extension API major is independent of the Omnigent package version. V1
changes are additive; authors must ignore unknown catalog fields. Breaking
changes require a new extension API major. Deprecations name the Omnigent
release in which removal is planned and remain supported through the stated
window.

Extension packages can run the same manifest and bundle checks in their own
focused tests without starting a server:

```python
from pathlib import Path
from omnigent.extensions import check_extension_package

check_extension_package(
    get_manifest(),
    project_root=Path(__file__).parents[2],
    package_root=Path(__file__).parent,
)
```

`activation_events`, `when`, and command records are reserved metadata only in
V1. Do not rely on them until a later API explicitly documents execution.
