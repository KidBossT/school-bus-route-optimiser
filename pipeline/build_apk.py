"""
Build the Bus GPS Android app (an APK holding only the tracker page).

    python pipeline/build_apk.py

The app is the same page as site/track.html, wrapped with Capacitor. Inside the
app the GPS comes from a native foreground service (plugin
@capacitor-community/background-geolocation), so a run keeps recording with the
screen locked or another app open. Output: dist/bus-gps.apk, signed for release.

Needs (see README, "Android app"):
    Node.js; JDK 21 and the Android SDK (platform 36, build-tools 35), found via
    JAVA_HOME / ANDROID_HOME or the folders in ANDROID_BUILD_DIR (.env);
    outputs/routes_data.json (for the route list).
On first run it creates the signing key (APK_KEYSTORE, APK_KEYSTORE_PASSWORD in
.env; keep that file and password safe: every update must be signed with it).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import secrets
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gmaps            # noqa: E402  (loads .env)
import build_routes     # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "android-app"
RES = APP / "android" / "app" / "src" / "main" / "res"
BLUE = (29, 78, 216, 255)
WIN = os.name == "nt"


def short(p: Path) -> str:
    """Windows 8.3 path (no spaces): Android's batch tools break on 'Tarav Gupta'."""
    if not WIN:
        return str(p)
    import ctypes
    buf = ctypes.create_unicode_buffer(1024)
    ctypes.windll.kernel32.GetShortPathNameW(str(p), buf, 1024)
    return buf.value or str(p)


def set_env(key: str, value: str) -> None:
    env = ROOT / ".env"
    with env.open("a", encoding="utf-8") as f:
        f.write(f"\n{key}={value}\n")
    os.environ[key] = value


def tools() -> dict:
    base = Path(os.environ.get("ANDROID_BUILD_DIR", Path.home() / "android-build"))
    java = Path(os.environ.get("JAVA_HOME") or base / "jdk21")
    sdk = Path(os.environ.get("ANDROID_HOME") or base / "sdk")
    for p, what in ((java / "bin", "JDK 21"), (sdk / "platforms" / "android-36", "Android platform 36")):
        if not p.exists():
            raise SystemExit(f"{what} not found at {p}")
    # Java's local sockets live in the temp folder; on Windows a long or spaced
    # path there fails with "Unable to establish loopback connection".
    tmp = base / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    jopts = f"-Djdk.net.unixdomain.tmpdir={short(tmp)}"
    return {"JAVA_HOME": short(java), "ANDROID_HOME": short(sdk), "ANDROID_SDK_ROOT": short(sdk),
            "ANDROID_USER_HOME": short(base / "user-home") if (base / "user-home").exists() else "",
            "GRADLE_OPTS": f"{jopts} -Djava.io.tmpdir={short(tmp)}", "JAVA_TOOL_OPTIONS": jopts}


# ---------------------------------------------------------------- web content
def web(data: dict) -> None:
    www = APP / "www"
    if www.exists():
        shutil.rmtree(www)
    www.mkdir(parents=True)
    build_routes.render_tracker(data, www)
    page = (www / "track.html").read_text(encoding="utf-8")
    # Capacitor's browser build provides registerPlugin, which the page uses for
    # the native plugins; the bridge Android injects does not include it.
    shutil.copy2(APP / "node_modules" / "@capacitor" / "core" / "dist" / "capacitor.js", www / "capacitor.js")
    assert page.count("<script>") == 1
    page = page.replace("<script>", '<script src="capacitor.js"></script>\n<script>', 1)
    (www / "index.html").write_text(page, encoding="utf-8")
    (www / "track.html").unlink()
    (www / "track-sw.js").unlink()             # no service worker inside the app


# ---------------------------------------------------------------- icon and splash
def pin(size: int, scale: float, bg) -> Image.Image:
    """White map pin on `bg` (None = transparent), drawn 4x and scaled down."""
    S = size * 4
    im = Image.new("RGBA", (S, S), bg or (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    cx, cy, r = S / 2, S / 2 - S * 0.07 * scale / 0.17, S * scale
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill="white")
    d.polygon([(cx - r * 0.86, cy + r * 0.5), (cx + r * 0.86, cy + r * 0.5), (cx, cy + r * 1.95)], fill="white")
    ri = r * 0.45
    d.ellipse([cx - ri, cy - ri, cx + ri, cy + ri], fill=BLUE if bg is None else bg)
    return im.resize((size, size), Image.LANCZOS)


def icons() -> None:
    dens = {"mdpi": 1, "hdpi": 1.5, "xhdpi": 2, "xxhdpi": 3, "xxxhdpi": 4}
    for name, k in dens.items():
        folder = RES / f"mipmap-{name}"
        legacy = pin(int(48 * k), 0.2, BLUE)
        legacy.save(folder / "ic_launcher.png")
        mask = Image.new("L", legacy.size, 0)
        ImageDraw.Draw(mask).ellipse([0, 0, legacy.size[0] - 1, legacy.size[1] - 1], fill=255)
        rnd = Image.new("RGBA", legacy.size, (0, 0, 0, 0)); rnd.paste(legacy, (0, 0), mask)
        rnd.save(folder / "ic_launcher_round.png")
        fg = pin(int(108 * k), 0.13, None)          # adaptive icon: keep inside the 66 dp safe zone
        fg.save(folder / "ic_launcher_foreground.png")
    (RES / "values" / "ic_launcher_background.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<resources>\n    <color name="ic_launcher_background">#1D4ED8</color>\n</resources>\n',
        encoding="utf-8")
    for png in RES.glob("drawable*/splash.png"):    # plain blue with the pin, at each existing size
        w, h = Image.open(png).size
        im = Image.new("RGBA", (w, h), BLUE)
        p = pin(min(w, h) // 3, 0.2, BLUE)
        im.paste(p, ((w - p.width) // 2, (h - p.height) // 2))
        im.save(png)


# ---------------------------------------------------------------- signing
def keystore(env: dict) -> tuple[str, str]:
    path = os.environ.get("APK_KEYSTORE", "").strip()
    pw = os.environ.get("APK_KEYSTORE_PASSWORD", "").strip()
    if path and pw and Path(path).exists():
        return path, pw
    base = Path(os.environ.get("ANDROID_BUILD_DIR", Path.home() / "android-build"))
    path = str(base / "busgps-release.jks")
    if Path(path).exists():
        raise SystemExit(f"{path} exists but APK_KEYSTORE / APK_KEYSTORE_PASSWORD are not in .env")
    pw = secrets.token_urlsafe(18)
    subprocess.run([str(Path(env["JAVA_HOME"]) / "bin" / "keytool"), "-genkeypair", "-keystore", path,
                    "-alias", "busgps", "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000",
                    "-storepass", pw, "-keypass", pw, "-dname", "CN=Bus GPS tracker, C=IN"],
                   check=True, capture_output=True)
    set_env("APK_KEYSTORE", path)
    set_env("APK_KEYSTORE_PASSWORD", pw)
    print(f"  created signing key {path} (password in .env). Back both up: updates need them.")
    return path, pw


# ---------------------------------------------------------------- build
def main() -> int:
    gmaps.load_env()
    env = {k: v for k, v in tools().items() if v}
    run_env = {**os.environ, **env}
    data = json.loads((ROOT / "outputs" / "routes_data.json").read_text(encoding="utf-8"))
    web(data)
    icons()
    subprocess.run("npx cap sync android", cwd=APP, shell=True, check=True, env=run_env,
                   stdout=subprocess.DEVNULL)
    (APP / "android" / "local.properties").write_text(
        "sdk.dir=" + env["ANDROID_HOME"].replace("\\", "\\\\").replace(":", "\\:") + "\n", encoding="utf-8")
    ks, pw = keystore(env)
    now = dt.datetime.now()
    code, name = int(now.strftime("%y%m%d%H")), "1." + now.strftime("%y%m%d.%H%M")
    gradlew = APP / "android" / ("gradlew.bat" if WIN else "gradlew")
    cmd = [short(gradlew), "assembleRelease", "--no-daemon", "-q",
           f"-PbusgpsVersionCode={code}", f"-PbusgpsVersionName={name}",
           f"-Pandroid.injected.signing.store.file={ks}", f"-Pandroid.injected.signing.store.password={pw}",
           "-Pandroid.injected.signing.key.alias=busgps", f"-Pandroid.injected.signing.key.password={pw}"]
    r = subprocess.run(cmd, cwd=APP / "android", env=run_env, capture_output=True, text=True)
    if r.returncode != 0:
        print((r.stdout + r.stderr)[-6000:], file=sys.stderr)
        return r.returncode
    apk = APP / "android" / "app" / "build" / "outputs" / "apk" / "release" / "app-release.apk"
    out = ROOT / "dist" / "bus-gps.apk"
    out.parent.mkdir(exist_ok=True)
    shutil.copy2(apk, out)
    with zipfile.ZipFile(out) as z:                 # the page inside must be the tracker, nothing else
        pages = [n for n in z.namelist() if n.startswith("assets/public/") and n.endswith(".html")]
        page = z.read("assets/public/index.html").decode("utf-8")
        has_core = "assets/public/capacitor.js" in z.namelist()
    assert pages == ["assets/public/index.html"], pages
    assert "BUS STOPPED" in page and "riders" not in page and "AIza" not in page
    assert has_core and '<script src="capacitor.js">' in page
    print(f"wrote {out} ({out.stat().st_size // 1024} KB), version {name} ({code})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
