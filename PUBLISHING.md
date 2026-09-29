# Publishing a plugin

A plugin repository is a plain, static directory tree served over HTTPS:

```
/index.json                              # catalog: every plugin id this repo carries
/plugins/<pluginId>/index.json           # full publish history for one plugin, newest first
/plugins/<pluginId>/<version>/<pluginId>-<version>.jar
```

See `RepositoryCatalogDto`/`RepositoryPluginVersionsDto` in `ares-core` for the exact JSON shape.

## Publishing to the official repository

1. Get in touch to agree on your plugin's `pluginId` and get its publish credential — you'll
   receive it out of band, not through an issue or PR.
2. Copy `ci.yml.template`, `release.yml.template`, and `publish-plugin.yml.template` from this
   repo into your own repo's `.github/workflows/` (drop the `.template` suffix).
3. Set these secrets on your repo — the first four come with your credential, the last pair *is*
   your credential:

   | Secret | |
   |---|---|
   | `ARES_REPO_BUCKET` | |
   | `ARES_REPO_ENDPOINT_URL` | |
   | `ARES_REPO_REGION` | |
   | `ARES_REPO_PUBLIC_BASE_URL` | |
   | `ARES_REPO_ACCESS_KEY_ID` / `ARES_REPO_SECRET_ACCESS_KEY` | your plugin's own, never shared with another plugin |

4. Run the **Release** workflow (`channel`: `release`/`rc`/`beta`/`alpha`) — it bumps your
   version, tags it, and publishes automatically. `plugin.json`'s own `version` field is always
   the source of truth for what gets published.

Your credential can only write under `/plugins/<your-id>/*` — you can't touch another plugin's
files or the shared catalog, and a new version is immutable once published (only cosmetic fields
like `displayName`/`vendor`/`icon` can be refreshed later, never the jar or its checksum).

## Compatibility fields `plugin.json` can declare

`minAresApiVersion`/`maxAresApiVersion`/`minAresUiVersion`/`maxAresUiVersion` (see
`PluginManifest`) bound which running `ares-core`/`ares-ui` versions can install a given version —
checked both when browsing and at install time. `dependsOn` entries
(`{"pluginId", "minVersion", "maxVersion"}`) work the same way against another already-installed
plugin's own version. Either bound may be omitted for "no constraint on that side".

## Running your own repository instead

Want to host a separate plugin repository rather than publishing to the official one? Serve the
same static layout above from any HTTPS host, then add it in your own Ares instance under
Administration → Plugin Repositories — no code change needed on the `ares-core` side.
