# Developing an Ares plugin

This is the reference for writing a plugin's own code — what to depend on, the `plugin.json`
schema, and every extension point a plugin can implement. See `PUBLISHING.md` for the other half
of the story: how a finished plugin actually gets published to a repository and installed.

## What to depend on

Every plugin depends on **`ares-sdk`** — a small, standalone module with zero dependency on
`ares-core` itself. It's the only committed, stable contract: the extension-point interfaces below
plus their own supporting DTOs/enums, nothing else. This is deliberately NOT "depend on ares-core
and use whatever you find" — see the next section for why that distinction is enforced.

```xml
<properties>
    <ares-sdk.version>1.0.0-alpha1</ares-sdk.version>
</properties>

<dependency>
    <groupId>com.martecyber.ares</groupId>
    <artifactId>ares-sdk</artifactId>
    <version>${ares-sdk.version}</version>
    <scope>provided</scope>
</dependency>
```

(`provided`, like every plugin dependency — these classes are on the classpath at runtime because
`ares-core` itself depends on `ares-sdk` too; a plugin JAR never bundles them.)

Look at this repo's own `ci.yml.template`/`release.yml.template`/`publish-plugin.yml.template` and
any existing `martecyber/ares-plugin-*` repo's `pom.xml` for a complete, working example.

### What belongs in `ares-sdk` (for anyone growing the SDK itself)

Two independent questions decide where a piece of code goes — get both right before adding
anything new to `ares-sdk`:

1. **Does it touch state, a database, or an external system?** If yes, it must be a plain
   **interface** (+ plain DTOs) in `ares-sdk`, with the real implementation as a `*Impl` class in
   `ares-core` (or, for something only one plugin needs, inside that plugin itself — see the
   second question). `ares-sdk` must never contain a class that opens a DB connection, calls a
   core-internal Spring service, or otherwise reaches into Ares's live state. Every facade listed
   below (`JobFacade`, `AssetFacade`, `DetectionFacade`, ...) follows this. The only concrete
   classes `ares-sdk` carries are things with no behavior to hide behind an interface in the first
   place: plain data shapes (`record`s like `ScopeEntryDto`), constants/vocabulary
   (`AssetType`, `WorkflowScope`), and pure, side-effect-free functions that only ever read/write
   in-memory data the caller already has (`ScannerParserUtils`, `ScopeNormalizer`,
   `SeverityThresholds`) — never something that reads or writes Ares's persisted state directly.
2. **Does more than one plugin actually need it?** `ares-sdk` is a *shared* contract. Something
   only one plugin uses — a REST client for that tool's own external API, a bit of parsing logic
   specific to that tool's own file format — is that plugin's own implementation detail and
   belongs inside the plugin itself, not in `ares-sdk`, regardless of how clean the code is. See
   `ares-plugin-caido`'s own `CaidoGraphQLClient` for a class that's entirely core-independent
   (pure `RestTemplate` wrapper) but still lives in the plugin, not the SDK, because nothing else
   needs it.

### `ares-sdk` vs depending on `ares-core` directly

**All 22 first-party plugins are fully `ares-sdk`-only** — no `ares-core` dependency in any
`pom.xml` at all, as of 2026-09-26. Beyond the simple `ImportParser`-only plugins, this now also
covers every plugin built as an `IntegrationActionHandler` + sync-job pair (`ares-plugin-action1`,
`-crowdstrike`, `-fortirecon`, `-qualys`, `-tenable`, `-greenbone`, `-caido`, `-shodan`, ...) via
the facade interfaces `ares-sdk` exposes:

- **`JobFacade`** (`com.martecyber.ares.jobs`) — create/update/read a tracked async job, check
  cancellation, without `JobRepository`/`Job` (JPA types) on your classpath.
- **`IntegrationFacade`** (`com.martecyber.ares.integrations`) — list/read configured integration
  instances, load decrypted credentials, record a sync attempt's outcome, resolve a project's
  grant(s) — all via plain `IntegrationView`/`GrantView` records, never the raw entities.
- **`ProjectFacade`** (`com.martecyber.ares.projects`) — resolve a project's organization id/type
  code, check existence.
- **`ProjectTypeFacade`** (`com.martecyber.ares.projects`) — register your own project type(s)
  (see "Registering a project type" below) — the mechanism `ares-plugin-bughunting` uses for `BH`.
- **`ScopeFacade`** (`com.martecyber.ares.projects`) — upsert/list/prune a project's scope entries.
- **`WorkflowFacade`** (`com.martecyber.ares.workflows`) — keep a managed, locked Workflow in sync
  with your own scheduling state.
- **`ProjectRulesFacade`** (`com.martecyber.ares.projects.rules`) — sync platform-sourced testing
  requirements (user-agent/header) into a project's rules.
- **`IngestFacade`** (`com.martecyber.ares.imports`) — persist a `ParseResult` built from a live
  API response (what an `ImportParser` gets for free at file-import time), returning a plain
  `IngestResult`.
- **`AssetFacade`** (`com.martecyber.ares.assets`) — resolve/create/link an asset one at a time
  (as opposed to `IngestFacade`'s batch `ParseResult`), grant project access, record a tool sighting.
- **`DetectionFacade`** (`com.martecyber.ares.detections`) — ingest one detection from a live API
  response, attach an HTTP request/response sample.
- **`CredentialCryptoFacade`** (`com.martecyber.ares.integrations`) — encrypt/decrypt a secret your
  plugin stores in its own table, reusing Ares's own credential key.
- **`PlatformFacade`** (`com.martecyber.ares.common`) — platform-wide settings (currently just the
  configured timezone).
- **`TargetResolverFacade`** (`com.martecyber.ares.agents.tasks`) — resolve a workflow target
  selector (`project_assets`/`scope_entries`) into concrete IP/domain/hostname strings, without
  needing the AQL/JPA-backed `TargetResolver` on your classpath.
- **`CveFacade`** (`com.martecyber.ares.kb.cve`) — bulk CVE lookups against Ares's own knowledge
  base, returning plain `CveInfo` records — useful for enriching a tool-reported CVE id that came
  with thin or absent severity/CVSS of its own (see `ares-plugin-shodan` for a worked example).

Each is a plain interface with a thin adapter bean living in `ares-core` (e.g. `JobFacadeImpl`
wraps the real `JobService`) — nothing else to configure, a plugin just constructor-injects the
facade type like any other bean. See any of the plugins listed above for a complete worked
example of the whole shape (handler + sync-job-handler + parsers) — `ares-plugin-caido` is the
most complete example, since it also owns its own tables (via hand-written `JdbcTemplate` DAOs,
see the next section) and its own REST controller.

### A plugin owning its own database table

A plugin can't ship its own Flyway migrations (Flyway's migration history is core-owned), so a
plugin-owned table is created idempotently (`CREATE TABLE IF NOT EXISTS`) from `PluginLifecycle
#onInstall`, and read/written via a hand-written `JdbcTemplate` DAO class (implementing {@code
PluginComponent}) instead of Spring Data JPA — Hibernate maps its entities once at boot, before any
plugin loads, so a plugin-defined `@Entity` would simply be invisible to it. See
`ares-plugin-bughunting`'s `ProjectBugHuntingProgram`/`ProjectBugHuntingProgramRepository` for the
simplest example, or `ares-plugin-caido`'s `CaidoApiIntegration`/`CaidoApiTask`/
`CaidoApiIntegrationGrant` (+ their own DAOs) for one with `jsonb` columns and foreign keys too.

### Registering a project type

A plugin can register its own project type(s) via `ProjectTypeFacade.ensure(ProjectTypeSpec)` from
`PluginLifecycle#onInstall`, and `ProjectTypeFacade.disable(code)` from `#onForget` (soft-disable
only — never deletes the row, or any project already using it). `ProjectTypeSpec.parentCode` nests
a type under an already-installed one (throws if the parent isn't installed — declare it as a
`dependsOn` so ordering is guaranteed); `continuousNumbering` opts the type into year-less,
continuous per-org project-code numbering (e.g. `ORG-BH-01` instead of `ORG-BH-25-01`). See
`ares-plugin-bughunting`'s `BugHuntingPluginLifecycle` (the root `"BH"` type) and
`ares-plugin-bughunting-hackerone`/`-intigriti`'s own lifecycles (a `"BH_H1"`/`"BH_INTG"` subtype
nested under it) for a complete worked example.

**Direct `ares-core` access is not a supported path for a third-party contribution.** Even though
`martecyber/ares-core`'s repo and its GitHub Package are both public, nothing about that surface is
a committed contract — it can change or disappear across `ares-core` versions with no notice, the
same way any internal implementation detail can regardless of who can read the source. If you're
contributing a plugin from outside the platform team and `ares-sdk` alone doesn't cover what you
need, say so — that's a real signal `ares-sdk`'s own surface should grow, not a reason to reach
around it.

## `plugin.json`

Every plugin JAR carries this file at its root (`src/main/resources/plugin.json`). Read by
`PluginLoader` before the JAR's classes are ever touched, so an incompatible/malformed plugin is
rejected without running a line of its code.

| Field | Type | Required | Meaning |
|---|---|---|---|
| `id` | string | yes | Stable identifier — the tool id used across the API/DB. Never changes once published. |
| `version` | string | yes | This build's own version (`MAJOR.MINOR.PATCH[-suffix]`, e.g. `1.0.0-alpha1`). Compared with the already-installed version's on every install attempt. |
| `displayName` | string | yes | Human label shown in the UI. |
| `vendor` | string | no | Shown next to the plugin in the admin UI and the marketplace. |
| `license` | string | no | Free text (e.g. `"Proprietary"`, `"MIT"`). |
| `description` | string | no | Shown in the marketplace's own preview panel — write this for a real reader, not just internal notes. |
| `sdkVersion` | string | yes | Which `ares-sdk` major generation this plugin targets. Exact-match gate today (a single string, e.g. `"1"`) — not yet a real range, see `PluginService`'s own doc comment for why. |
| `icon` / `iconLight` | string | no | A URL, or an `ares-ui`-relative path (e.g. `/img/sources/nmap.svg`) if you've added a bundled icon there. `iconLight` is an optional light-theme override; omit it if your `icon` already reads fine on both themes. |
| `dependsOn` | array of `{pluginId, minVersion?, maxVersion?}` | no | Other plugins this one requires already installed + enabled. Either version bound may be omitted for "no constraint on that side". |
| `providesExtensionPoints` | array of FQCN strings | no | Interfaces *this* plugin declares for other plugins to implement — see `PluginExtensionRegistry` below. Empty unless you're building a "base" plugin others extend. |
| `ownedPathPrefixes` | array of strings | no | URL path prefixes your own `PluginRestController`(s) own (e.g. `/api/v1/projects/*/my-thing`) — lets a request to a disabled/uninstalled plugin's route get a clear "this plugin isn't loaded" error instead of a bare 404. |
| `minAresApiVersion` / `maxAresApiVersion` | string | no | Inclusive bounds on the running instance's own product version (`ares-core`'s `api` version — see `VersionsController` — NOT `sdkVersion`, which gates the plugin SPI itself). Omit either side for "no constraint on that side". |
| `minAresUiVersion` / `maxAresUiVersion` | string | no | Same idea, against the `ui` product version. |

## Extension points

A plugin implements one or more of these. Each is discovered via a
`META-INF/services/<fully-qualified-interface-name>` file listing your implementation
class(es), one per line — the standard `java.util.ServiceLoader` convention, though
`PluginLoader` does the actual discovery+wiring itself (your classes still become real,
constructor-injected Spring beans, wired against `ares-core`'s own application context).

### `ImportParser`

Parses one file format from one tool into assets/detections. The most common extension point —
every "import-only" plugin (testssl, PurpleKnight, Trivy, ...) is just one or more of these.

```java
public class MyToolJsonParser implements ImportParser {
    public String getToolId() { return "mytool"; }
    public String getFormatId() { return "json"; } // default: "default" — set this if you have more than one format
    public String getDisplayName() { return "MyTool JSON"; }
    public String[] getSupportedExtensions() { return new String[]{".json"}; }
    public boolean validate(byte[] content) { /* quick sniff test */ return true; }
    public ParseResult parse(byte[] content) {
        ParseResult result = new ParseResult();
        result.addAsset(new ParsedAsset("1.2.3.4", AssetType.IP));
        result.addDetection(new ParsedDetection("Something found", "high", "description",
            "1.2.3.4", "mytool-something-found", content == null ? "{}" : new String(content)));
        return result;
    }
}
```

`META-INF/services/com.martecyber.ares.imports.ImportParser`

### `IntegrationActionHandler`

Wires a live integration (an API you call, credentials a user configures) into the
`ACTION_INTEGRATION_CALL` workflow node type — "sync assets", "launch a scan", anything a workflow
can trigger and later poll for completion. See any of `ares-plugin-caido`/`ares-plugin-tenable`/
`ares-plugin-qualys` for a full worked example; the interface itself
(`com.martecyber.ares.workflows.integrations.IntegrationActionHandler`) documents every method's
contract in its own javadoc.

`META-INF/services/com.martecyber.ares.workflows.integrations.IntegrationActionHandler`

### `IntegrationClient`

The connector strategy for a "Data Source" integration type shown in the org's own integrations
list (test connection, optional picker items, optional capability detection). Simpler and more
data-source-shaped than `IntegrationActionHandler` — most tool connectors are one of these, not
the other.

`META-INF/services/com.martecyber.ares.integrations.tools.IntegrationClient`

### `DetectionUrlReferenceParser`

Optional: if your tool's raw JSON output embeds reference/documentation URLs in a known,
structured field (nuclei's `info.reference[]`, WPScan's `references.url[]`), implement this so
Ares extracts them onto the Detection automatically. Skip it entirely if your tool doesn't have
such a field — nothing else regresses.

```java
public class MyToolUrlReferenceParser implements DetectionUrlReferenceParser {
    public String getToolId() { return "mytool"; } // must match your ImportParser's own tool id
    public Set<String> extractUrls(String rawJson) { /* ... */ return Set.of(); }
}
```

`META-INF/services/com.martecyber.ares.detections.DetectionUrlReferenceParser`

### `PluginComponent`

Not an extension point of its own — a marker for an internal helper bean your other beans
constructor-inject (a `@Service`-style class that isn't itself one of the other interfaces here).
Without this, `PluginLoader` never instantiates it at all, since it only calls `createBean` for
classes it discovers via a known convention. List your internal helpers here **in dependency
order** — a bean must appear before anything that constructor-injects it.

`META-INF/services/com.martecyber.ares.plugins.PluginComponent`

### `PluginLifecycle`

Optional install/uninstall hook, for a plugin that owns its own database table(s) and/or seed
rows. A plugin can't ship its own Flyway migrations (Flyway's migration history is core-owned), so
manage your own table via plain `JdbcTemplate` SQL instead — `CREATE TABLE IF NOT EXISTS`-style,
idempotent, since `onInstall` can run more than once (every fresh install, not every re-enable).

```java
public class MyPluginLifecycle implements PluginLifecycle {
    public void onInstall(JdbcTemplate jdbc) {
        jdbc.execute("CREATE TABLE IF NOT EXISTS ares.my_plugin_data (id BIGSERIAL PRIMARY KEY, ...)");
    }
    public void onForget(JdbcTemplate jdbc) {
        jdbc.execute("DROP TABLE IF EXISTS ares.my_plugin_data");
    }
}
```

`META-INF/services/com.martecyber.ares.plugins.PluginLifecycle` — at most one per plugin.

### `PluginRestController`

Marker your own `@RestController` class implements (alongside its usual `@RestController`/
`@RequestMapping` annotations) so `PluginLoader` dynamically registers its routes with Spring
MVC after boot — a plugin loaded after startup is otherwise invisible to the classpath-scanned
routing table Spring MVC normally builds once, at boot.

`META-INF/services/com.martecyber.ares.plugins.PluginRestController`

### `PluginExtensionRegistry<T>`

Lets a "base" plugin (e.g. `ares-plugin-bughunting`) define its own extension point for OTHER
plugins to implement, instead of only the two built into core above. Declare your own interface,
list its FQCN in your `plugin.json`'s own `providesExtensionPoints`, and ship one bean implementing
`PluginExtensionRegistry<YourInterface>`. A dependent plugin (one listing you under `dependsOn`)
then just implements your interface and lists its own implementation under
`META-INF/services/<your-interface's-FQCN>` — no coordination needed beyond that. See
`ares-plugin-bughunting`'s own `BugHuntingClient`/`BugHuntingClientRegistry` for a complete,
working example, and `ares-plugin-bughunting-hackerone`/`-intigriti` for the dependent side.

`META-INF/services/com.martecyber.ares.plugins.PluginExtensionRegistry`

## Agent-executed scan tools: `agent-tools.json`

A different, pure-data convention (no Java interface at all) for a tool `ares-agent` itself
executes (nmap, wpscan, ffuf, ...) rather than a file you import after running it yourself:
declares the command-line shape ares-agent uses to build and run the tool, plus which
scope/asset types it's valid against. See `ares-plugin-nmap/src/main/resources/agent-tools.json`
for a complete, real example — this is the easiest way to understand the shape, and most new
agent-executed-tool plugins start by copying it.

## See also

- `PUBLISHING.md` — the repository format, how a version actually gets published, and how to
  accept a third-party contribution.
