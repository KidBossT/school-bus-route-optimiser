"""
Build the password-locked web copy of the route planner into site/.

    python pipeline/build_site.py

The planner shows where and when the school's children are picked up, so the
published copy is encrypted with StatiCrypt (AES-256, key derived from the
password with PBKDF2). Anyone can download site/index.html; only someone with
the password can read it. GitHub Pages and Vercel both serve site/ as-is.

Needs, in .env (git-ignored):
    SITE_PASSWORD   the password readers type
    SITE_SALT       32 hex characters; created on first run. Keeping it fixed
                    lets "Remember me" survive a rebuild.
and Node.js (StatiCrypt runs through npx; nothing is installed in the repo).

Reads outputs/routes_data.json, so run build_routes.py first after a data change.
The Google Maps browser key inside the page must also allow the site's web
addresses (see README, "Publishing").
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gmaps            # noqa: E402  (loads .env)
import build_routes     # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
STATICRYPT = "staticrypt@3.5.4"
ROBOTS = "User-agent: *\nDisallow: /\n"     # keep the lock page out of search results
NOINDEX = '<meta name="robots" content="noindex, nofollow">'


def ensure_salt() -> str:
    salt = os.environ.get("SITE_SALT", "").strip()
    if len(salt) == 32:
        return salt
    salt = secrets.token_hex(16)
    env = ROOT / ".env"
    with env.open("a", encoding="utf-8") as f:
        f.write(f"\nSITE_SALT={salt}\n")
    print(f"  created SITE_SALT in {env.name}")
    return salt


def main() -> int:
    gmaps.load_env()
    password = os.environ.get("SITE_PASSWORD", "").strip()
    if len(password) < 10:
        print("SITE_PASSWORD is missing from .env or shorter than 10 characters.", file=sys.stderr)
        return 1
    data = json.loads((ROOT / "outputs" / "routes_data.json").read_text(encoding="utf-8"))
    site = ROOT / "site"
    with tempfile.TemporaryDirectory() as tmp:
        page = build_routes.render(data, Path(tmp) / "index.html")
        cmd = ["npx", "--yes", STATICRYPT, str(page), "-d", str(site), "-c", "false",
               "-s", ensure_salt(), "--short", "--remember", "30",
               "--template-title", "Bus route planner",
               "--template-instructions", "This planner shows children's pickup points, so it is "
                                          "password-protected. Ask the project owner for access.",
               "--template-button", "Open planner",
               "--template-color-primary", "#3b82f6", "--template-color-secondary", "#0b0f17"]
        env = {**os.environ, "STATICRYPT_PASSWORD": password}
        r = subprocess.run(cmd, env=env, shell=(os.name == "nt"), capture_output=True, text=True)
        if r.returncode != 0:
            print(r.stdout + r.stderr, file=sys.stderr)
            return r.returncode
    (site / "robots.txt").write_text(ROBOTS, encoding="utf-8")
    out = site / "index.html"
    text = out.read_text(encoding="utf-8")
    # robots.txt only counts at a domain's root, which a GitHub Pages project
    # site is not, so the lock page also says it itself.
    if NOINDEX not in text:
        text = text.replace("<head>", "<head>\n    " + NOINDEX, 1)
        out.write_text(text, encoding="utf-8")
    leaks = [k for k in ("GOOGLE_MAPS", "AIza", "riders", "Prometheus") if k in text]
    if leaks:                      # the plain page must never reach site/
        out.unlink()
        print(f"site/index.html still contained {leaks}; not encrypted? Deleted it.", file=sys.stderr)
        return 1
    print(f"wrote {out} ({out.stat().st_size // 1024} KB, encrypted)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
