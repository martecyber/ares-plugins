#!/usr/bin/env python3
"""Publishes one built plugin JAR to the official repository bucket — writes ONLY within this
plugin's own `plugins/<id>/` subtree (see ares-core/.../plugins/PluginBrowseService.java's own
doc comment for the full repository layout). Deliberately never touches the shared root
`/index.json` catalog — see rebuild_catalog.py (in ../_admin-tools/) for that, and
ares-plugins/PUBLISHING.md's "Accepting a third-party plugin" section for why the two are split:
this script is what a contributor's own CI runs, with a credential scoped (via an OVH IAM policy)
to read/write only their own plugin's prefix — it must never be *able* to touch another plugin's
files or the shared catalog, not just be asked nicely not to.

This is a TEMPLATE — copied as-is into each plugin's own (eventually separate) repository
alongside publish-plugin.yml.template, not run from here. It has not been exercised against a
real bucket yet (no plugin has actually been split into its own repo / no bucket exists yet) —
review carefully before the first real use, especially the boto3 credential/endpoint wiring.

Reference setup: OVH Object Storage (S3 API) as the bucket, a bunny.net Pull Zone in front of it
for the public read domain/CDN/cache — see ares-plugins/PUBLISHING.md for the full provisioning
guide and why this pairing. OVH needs `region_name` + path-style addressing (its endpoint doesn't
do virtual-hosted-style bucket subdomains) — both set explicitly below rather than left to
boto3's defaults, which assume AWS.

Usage: publish_plugin.py --jar target/<id>-<version>.jar --plugin-json src/main/resources/plugin.json [--release-notes "..."]
Required env vars: ARES_REPO_BUCKET, ARES_REPO_ENDPOINT_URL, ARES_REPO_REGION,
AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY — the last two are this plugin's own SCOPED credential
(see scoped-user-policy.json.template), not the admin one rebuild_catalog.py uses.
"""
import argparse
import hashlib
import json
import sys
import os
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import urlopen

import boto3
from botocore.client import Config

MANIFEST_FIELDS = (
    "sdkVersion", "minAresApiVersion", "maxAresApiVersion",
    "minAresUiVersion", "maxAresUiVersion", "dependsOn",
)


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_json(base_url: str, path: str, default):
    """GETs a repo file over plain HTTPS (the bucket's own public read endpoint, NOT the
    S3 API — matches how ares-core itself reads a repository) rather than a signed S3 GET, so
    this needs no read permission on the S3 side at all (this plugin's own scoped credential
    only ever needs to Put). Missing file (this plugin's first-ever publish) returns `default` —
    treating 403 the same as 404: OVH returns 403 Forbidden (not 404) for an anonymous GET of an
    object that was never uploaded with a public-read ACL, rather than confirming it's absent,
    to avoid leaking existence to unauthenticated callers. Confirmed empirically 2026-09 (a
    brand-new plugin's very first publish — nothing has ever been PUT under its prefix yet, so
    there's no ACL for OVH to have honored either way)."""
    url = base_url.rstrip("/") + "/" + path.lstrip("/")
    try:
        with urlopen(url) as resp:
            return json.load(resp)
    except HTTPError as e:
        if e.code in (403, 404):
            return default
        raise


def put(s3, bucket: str, key: str, body: bytes, content_type: str):
    s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type, ACL="public-read")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jar", required=True, help="path to the built plugin JAR")
    ap.add_argument("--plugin-json", required=True, help="path to this plugin's own plugin.json (source, not from inside the JAR)")
    ap.add_argument("--release-notes", default=None)
    args = ap.parse_args()

    bucket = os.environ["ARES_REPO_BUCKET"]
    endpoint_url = os.environ["ARES_REPO_ENDPOINT_URL"]  # S3 API endpoint (signed writes)
    region = os.environ["ARES_REPO_REGION"]  # e.g. "gra" — OVH requires this even though there's one bucket namespace per endpoint
    # Virtual-hosted-style default (bucket as a subdomain) — matches the addressing style forced
    # below; some older-generation OVH regions also accept path-style, but newer 3-AZ regions
    # reject it outright ("Not S3 request"), confirmed empirically 2026-09.
    public_base_url = os.environ.get("ARES_REPO_PUBLIC_BASE_URL", endpoint_url.replace("https://", f"https://{bucket}."))

    manifest = json.load(open(args.plugin_json))
    plugin_id = manifest["id"]
    version = manifest["version"]
    checksum = sha256_of(args.jar)

    s3 = boto3.client(
        "s3", endpoint_url=endpoint_url, region_name=region,
        # Forced virtual-hosted (<bucket>.<endpoint>/<key>), not boto3's path-style default —
        # newer OVH 3-AZ regions reject path-style outright ("Not S3 request"), confirmed
        # empirically 2026-09. Older-generation regions reportedly accept path-style too, but
        # virtual-hosted works everywhere, so there's no reason to branch on region.
        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
    )

    plugin_index_path = f"plugins/{plugin_id}/index.json"
    plugin_index = fetch_json(public_base_url, plugin_index_path, {"id": plugin_id, "versions": []})
    # Refreshed from the manifest on every publish (self-healing if displayName/vendor/icon/
    # description ever change) — see RepositoryPluginVersionsDto's own doc comment for why these
    # live at this top level rather than per-version.
    plugin_index["displayName"] = manifest.get("displayName")
    plugin_index["vendor"] = manifest.get("vendor")
    plugin_index["icon"] = manifest.get("icon")
    plugin_index["iconLight"] = manifest.get("iconLight")
    plugin_index["description"] = manifest.get("description")

    jar_key = f"plugins/{plugin_id}/{version}/{plugin_id}-{version}.jar"

    if any(v["version"] == version for v in plugin_index["versions"]):
        # That release's own facts (checksum/compat ranges/JAR) are immutable once published —
        # refusing to touch them is deliberate. The plugin's identity fields above (displayName/
        # vendor/icon) aren't tied to any one version though, so still re-upload the index with
        # those refreshed even when there's no new release to add — otherwise fixing a typo'd
        # displayName would be stuck forever behind a version bump nobody actually needs.
        put(s3, bucket, plugin_index_path, json.dumps(plugin_index, indent=2).encode(), "application/json")
        print(f"Version {version} of '{plugin_id}' is already published — left it as-is, "
              "only refreshed displayName/vendor/icon.")
        return 0

    new_entry = {
        "version": version,
        "downloadUrl": jar_key,  # relative — resolved against the repo's own base URL, see PluginRepositoryClient
        "checksumSha256": checksum,
        "publishedAt": datetime.now(timezone.utc).isoformat(),
        "releaseNotes": args.release_notes,
        "yanked": False,
        **{f: manifest.get(f) for f in MANIFEST_FIELDS},
    }
    # Newest first — see RepositoryPluginVersionsDto's own doc comment.
    plugin_index["versions"].insert(0, new_entry)

    with open(args.jar, "rb") as f:
        put(s3, bucket, jar_key, f.read(), "application/java-archive")
    put(s3, bucket, plugin_index_path, json.dumps(plugin_index, indent=2).encode(), "application/json")

    print(f"Published {plugin_id} {version} -> {public_base_url}/{jar_key}")
    print("NOTE: the shared catalog (/index.json) is not updated by this script — the repository "
          "owner needs to run rebuild_catalog.py (or wait for its scheduled run) before this "
          "version shows up in Browse.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
