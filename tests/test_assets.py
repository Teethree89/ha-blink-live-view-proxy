"""Checks on the shipped YAML, JSON and generated dashboards.

No Home Assistant, no Blink account, no network. Needs PyYAML. Run from the
repo root:

    python tests/test_assets.py

Every check here exists because something actually broke:

  * button-card styles were written `card: [[height, 74px]]`, which YAML parses
    as a list of lists. button-card wants a list of single-key maps, so every
    height and font-size was silently discarded and a lone pill ballooned to
    fill a whole masonry column.
  * hacs.json carried `domains` and `iot_class`, which are manifest.json keys.
    HACS rejects unknown keys outright rather than ignoring them.
  * manifest.json keys have to be domain, name, then strictly alphabetical, and
    an easy wrong guess is Home Assistant core's own ordering, which differs.
  * the generator's three output shapes each have a different root, and getting
    one wrong produces YAML the dashboard editor refuses.
  * the installer has to write the proxy API token to exactly the path the
    systemd unit reads, owner-only, and must never rotate one already in use:
    the Home Assistant integration holds the only other copy.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("PyYAML is required: pip install pyyaml")

ROOT = pathlib.Path(__file__).resolve().parent.parent

FAILURES: list[str] = []
CHECKS = 0


def check(condition: bool, label: str) -> None:
    global CHECKS
    CHECKS += 1
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}")
        FAILURES.append(label)


def tracked(*patterns: str) -> list[pathlib.Path]:
    """Files git knows about, so untracked scratch is never validated."""
    out = subprocess.run(
        ["git", "ls-files", *patterns],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.split()
    return [ROOT / name for name in out]


def walk(node, path=""):
    """Yield (path, key, value) for every mapping entry in a document."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield path, key, value
            yield from walk(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from walk(value, f"{path}[{index}]")


def test_yaml_parses() -> None:
    print("\nYAML parses")
    for path in tracked("*.yaml", "*.yml"):
        try:
            yaml.safe_load(path.read_text())
            ok = True
        except yaml.YAMLError as error:
            ok = False
            print(f"        {error}")
        check(ok, path.relative_to(ROOT).as_posix())


def test_json_parses() -> None:
    print("\nJSON parses")
    for path in tracked("*.json"):
        try:
            json.loads(path.read_text())
            ok = True
        except json.JSONDecodeError as error:
            ok = False
            print(f"        {error}")
        check(ok, path.relative_to(ROOT).as_posix())


def test_button_card_styles() -> None:
    """`styles: {card: [[a, b]]}` is a list of lists and silently does nothing."""
    print("\nbutton-card styles are lists of single-key maps")
    bad: list[str] = []
    for path in tracked("*.yaml", "*.yml"):
        try:
            doc = yaml.safe_load(path.read_text())
        except yaml.YAMLError:
            continue
        for where, key, value in walk(doc, path.relative_to(ROOT).as_posix()):
            if key != "styles" or not isinstance(value, dict):
                continue
            for style_key, style_value in value.items():
                if isinstance(style_value, list) and any(
                    isinstance(entry, list) for entry in style_value
                ):
                    bad.append(f"{where}.styles.{style_key}")
    for entry in bad:
        print(f"        {entry}")
    check(not bad, f"no list-of-lists styles ({len(bad)} found)")


def test_hacs_json() -> None:
    print("\nhacs.json")
    data = json.loads((ROOT / "hacs.json").read_text())
    # The keys HACS accepts for an integration. Anything else is rejected.
    allowed = {
        "name", "content_in_root", "country", "filename", "hacs",
        "hide_default_branch", "homeassistant", "persistent_directory",
        "render_readme", "zip_release",
    }
    extra = sorted(set(data) - allowed)
    for key in extra:
        print(f"        unexpected key: {key}")
    check(not extra, "no keys outside the HACS schema")
    check("name" in data, "has a name")


def test_manifest() -> None:
    print("\nmanifest.json")
    path = ROOT / "custom_components/blink_liveview_proxy/manifest.json"
    data = json.loads(path.read_text())
    keys = list(data)

    check(keys[:2] == ["domain", "name"], "domain and name come first")
    rest = keys[2:]
    check(rest == sorted(rest), "remaining keys are alphabetical")

    for required in ("domain", "name", "documentation", "codeowners", "version"):
        check(required in data, f"has {required}")

    # The integration registers HTTP views, so http is a hard dependency.
    # hassfest fails the build without it.
    check("http" in data.get("dependencies", []), "declares the http dependency")


def test_proxy_pill_keeps_its_mark() -> None:
    """The proxy pill shows the same icon whether the proxy is up or down.

    It used to swap to mdi:cctv-off, which is not a variant of the mark but a
    different object - a dome camera beside a webcam - so the pill stopped
    looking like this integration exactly when someone was reading it. A
    slashed variant of the mark was tried and rejected: with no knocked-out
    gap around the slash it merges into the rings and is unreadable at the 24
    and 40px the sidebar and the pill actually draw.

    So colour carries the state, and that is only safe because show_state
    prints the word underneath. If show_state ever goes, this decision has to
    be revisited - hence it is asserted here too.
    """
    print("\nthe proxy pill keeps its mark in both states")
    for path in tracked("examples/*.yaml"):
        try:
            doc = yaml.safe_load(path.read_text())
        except yaml.YAMLError:
            continue
        name = path.relative_to(ROOT).as_posix()
        for _where, key, value in walk(doc, name):
            # The pill is the button-card bound to the proxy health sensor.
            if key != "entity" or value != "binary_sensor.blink_liveview_proxy":
                continue
            break
        else:
            continue

        text = path.read_text()
        check(
            "mdi:cctv-off" not in text,
            f"{name} does not swap the pill for a foreign glyph",
        )
        # Both the base icon and every state override are the mark.
        icons = {
            value
            for _where, key, value in walk(doc, name)
            if key == "icon" and isinstance(value, str) and "cctv" in value
        }
        check(not icons, f"{name} has no leftover cctv icon ({sorted(icons)})")
        check(
            "icon: blink:logo" in text,
            f"{name} uses the shipped mark on the pill",
        )
        check(
            "show_state: true" in text,
            f"{name} prints the state as words, so colour is not carrying it alone",
        )


def test_translations_shipped() -> None:
    """strings.json is a build-time file. Custom integrations must ship the
    translation Home Assistant actually reads, translations/en.json, or every
    label in the config flow, the options flow and the repair issues renders
    as its raw key. Nothing in Home Assistant or HACS reports that."""
    print("\ntranslations are shipped, and match the source strings")
    component = ROOT / "custom_components/blink_liveview_proxy"
    source = json.loads((component / "strings.json").read_text())
    shipped_path = component / "translations/en.json"
    check(shipped_path.exists(), "translations/en.json exists")
    if shipped_path.exists():
        shipped = json.loads(shipped_path.read_text())
        check(shipped == source, "translations/en.json says exactly what strings.json says")
    icons = component / "frontend/blink-liveview-icons.js"
    check(icons.exists() and "customIcons" in icons.read_text(),
          "the blink: icon set is shipped, so the sidebar entry has its mark")


def test_generator_shapes() -> None:
    """--demo needs no proxy, so the three shapes are checkable here."""
    print("\ngenerate-dashboard.py --demo")
    script = ROOT / "scripts/generate-dashboard.py"
    expected = {
        "dashboard": lambda d: isinstance(d, dict) and "views" in d,
        "view": lambda d: isinstance(d, list) and "title" in d[0],
        "card": lambda d: isinstance(d, dict) and "type" in d and "views" not in d,
    }
    for shape, predicate in expected.items():
        result = subprocess.run(
            [sys.executable, str(script), "--demo", "--format", shape],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"        exited {result.returncode}: {result.stderr.strip()[:200]}")
            check(False, f"--format {shape} runs")
            continue
        try:
            doc = yaml.safe_load(result.stdout)
        except yaml.YAMLError as error:
            print(f"        {error}")
            check(False, f"--format {shape} emits valid YAML")
            continue
        check(predicate(doc), f"--format {shape} has the right root")

    # The made-up inventory is the point: no real cameras in the repo.
    demo = subprocess.run(
        [sys.executable, str(script), "--demo", "--format", "card"],
        capture_output=True, text=True, check=True,
    ).stdout
    check("front_door" in demo, "--demo uses the stand-in inventory")


def test_proxy_copies_match() -> None:
    """proxy/ and addon/proxy/ carry the same source and must not diverge.

    A pull request once added RTSP support to the add-on copy only, which
    would have left every Linux service install unable to use those cameras.
    Nothing caught it but a manual diff.
    """
    print("\nproxy/ and addon/proxy/ are identical")
    import filecmp

    left = ROOT / "proxy/blink_proxy"
    right = ROOT / "addon/proxy/blink_proxy"

    def compare(a: pathlib.Path, b: pathlib.Path, prefix: str = "") -> list[str]:
        result = filecmp.dircmp(a, b, ignore=["__pycache__"])
        problems = [f"{prefix}{name}: only in proxy/" for name in result.left_only]
        problems += [f"{prefix}{name}: only in addon/" for name in result.right_only]
        problems += [f"{prefix}{name}: contents differ" for name in result.diff_files]
        for name in result.common_dirs:
            problems += compare(a / name, b / name, f"{prefix}{name}/")
        return problems

    problems = compare(left, right)
    for entry in problems:
        print(f"        {entry}")
    check(not problems, f"the two proxy copies match ({len(problems)} differences)")

    # Same for the two example configs, which drifted once already.
    same = filecmp.cmp(
        ROOT / "proxy/config.example.json",
        ROOT / "addon/proxy/config.example.json",
        shallow=False,
    )
    check(same, "the two config.example.json files match")


def _token_block() -> str:
    """Pull the installer's token step out so it can run without root."""
    text = (ROOT / "scripts/install-proxy.sh").read_text()
    start = text.index('ENV_FILE="$ETC_DIR/blink-liveview-proxy.env"')
    end = text.index("\nfi\n", start) + len("\nfi\n")
    return "set -euo pipefail\n" + text[start:end]


def _run_block(script: str, etc_dir: pathlib.Path, **env_extra: str) -> None:
    subprocess.run(
        ["bash", "-c", script],
        check=True,
        env={"PATH": os.environ["PATH"], "ETC_DIR": str(etc_dir), **env_extra},
    )


def _run_token_block(etc_dir: pathlib.Path, **env_extra: str) -> None:
    _run_block(_token_block(), etc_dir, **env_extra)


def test_install_token() -> None:
    print("\ninstaller provisions the proxy API token")

    unit = (ROOT / "systemd/blink-liveview-proxy.service").read_text()
    env_line = next(
        (line for line in unit.splitlines() if line.startswith("EnvironmentFile=")), ""
    )
    check(
        env_line.split("=", 1)[-1].lstrip("-")
        == "/etc/blink-liveview-proxy/blink-liveview-proxy.env",
        "the unit reads the env file the installer writes",
    )

    with tempfile.TemporaryDirectory() as tmp:
        etc = pathlib.Path(tmp)
        env_file = etc / "blink-liveview-proxy.env"

        _run_token_block(etc)
        first = env_file.read_text()
        check(
            re.fullmatch(r"BLINK_PROXY_TOKEN=[0-9a-f]{64}\n", first) is not None,
            "a fresh install writes a 256-bit random token",
        )
        check(oct(env_file.stat().st_mode)[-3:] == "600", "the token file is owner-only")

        _run_token_block(etc)
        check(env_file.read_text() == first, "re-running the installer never rotates it")

    with tempfile.TemporaryDirectory() as tmp:
        etc = pathlib.Path(tmp)
        env_file = etc / "blink-liveview-proxy.env"
        env_file.write_text("BLINK_USERNAME=you@example.com\n")
        env_file.chmod(0o644)

        _run_token_block(etc)
        contents = env_file.read_text()
        check(
            "BLINK_USERNAME=you@example.com" in contents
            and "BLINK_PROXY_TOKEN=" in contents,
            "an existing env file keeps its other variables",
        )
        check(
            oct(env_file.stat().st_mode)[-3:] == "600",
            "a loose env file is tightened before a token lands in it",
        )

    with tempfile.TemporaryDirectory() as tmp:
        etc = pathlib.Path(tmp)
        _run_token_block(etc, BLINK_PROXY_TOKEN="supplied-by-the-operator")
        check(
            (etc / "blink-liveview-proxy.env").read_text()
            == "BLINK_PROXY_TOKEN=supplied-by-the-operator\n",
            "an operator-supplied token is used as-is",
        )

    installer = (ROOT / "scripts/install-proxy.sh").read_text()
    check(
        "NEW_TOKEN" not in installer and "TOKEN_VALUE" not in installer,
        "the generated token is never held in a shell variable that could echo",
    )
    message = installer[installer.index("cat <<MSG") :]
    check(
        "$TOKEN_NOTE" in message and "sed -n 's/^BLINK_PROXY_TOKEN=//p'" in message,
        "the installer reports where the token is, and how to read it back",
    )


def _config_block() -> str:
    """Pull the installer's config-writing step out so it can run unprivileged."""
    text = (ROOT / "scripts/install-proxy.sh").read_text()
    start = text.index('PROXY_PORT="${PROXY_PORT:-8088}"')
    end = text.index("\nfi\n", text.index('chmod 0600 "$ETC_DIR/config.json"'))
    return f'set -euo pipefail\nROOT="{ROOT}"\n' + text[start:end] + "\nfi\n"


def test_install_writes_config() -> None:
    print("\ninstaller writes a working config unattended")

    with tempfile.TemporaryDirectory() as tmp:
        etc = pathlib.Path(tmp)
        _run_block(_config_block(), etc)
        config = json.loads((etc / "config.json").read_text())
        check(config["cameras"] == {}, "no example camera is left behind to fail")
        check(config["host"] == "0.0.0.0", "it binds the LAN by default, now that a token is always set")
        check(config["port"] == 8088, "the default port is written")
        check(
            oct((etc / "config.json").stat().st_mode)[-3:] == "600",
            "the config file is owner-only",
        )

        (etc / "config.json").write_text(json.dumps({"port": 9999}))
        _run_block(_config_block(), etc)
        check(
            json.loads((etc / "config.json").read_text()) == {"port": 9999},
            "an existing config is never rewritten",
        )

    with tempfile.TemporaryDirectory() as tmp:
        etc = pathlib.Path(tmp)
        _run_block(_config_block(), etc, BIND_HOST="127.0.0.1", PROXY_PORT="9099")
        config = json.loads((etc / "config.json").read_text())
        check(
            config["host"] == "127.0.0.1" and config["port"] == 9099,
            "BIND_HOST and PROXY_PORT are honoured",
        )

    installer = (ROOT / "scripts/install-proxy.sh").read_text()
    message = installer[installer.index("cat <<MSG") :]
    check(
        "systemctl restart blink-liveview-proxy.service" in installer,
        "the installer starts the service instead of asking the reader to",
    )
    check(
        "command -v apt-get" in installer and "INSTALL_DEPS:-1" in installer,
        "missing ffmpeg/python3-venv are installed, and that step is skippable",
    )
    check(
        "Edit $ETC_DIR/config.json" not in message,
        "the closing message asks for no file editing",
    )


def test_addon_token_handoff() -> None:
    print("\nadd-on hands its token to the integration")

    run_sh = (ROOT / "addon/run.sh").read_text()
    const = (ROOT / "custom_components/blink_liveview_proxy/const.py").read_text()
    config_flow = (ROOT / "custom_components/blink_liveview_proxy/config_flow.py").read_text()
    addon_config = yaml.safe_load((ROOT / "addon/config.yaml").read_text())

    handoff = re.search(r'TOKEN_HANDOFF_FILE = "([^"]+)"', const)
    check(handoff is not None, "the integration names the handoff file")
    if handoff:
        check(
            f"$HA_CONFIG/{handoff.group(1)}" in run_sh,
            "the add-on writes exactly the file the integration reads",
        )
    check(
        "homeassistant_config:rw" in addon_config["map"],
        "the add-on maps the directory it writes that file into",
    )
    check(
        "bashio::config.has_value 'proxy_api_token'" in run_sh
        and "secrets.token_hex(32)" in run_sh,
        "an empty token option is provisioned rather than refused",
    )
    check(
        "/data/proxy-token" in run_sh,
        "the generated token persists across restarts and updates",
    )
    check(
        not re.search(r"bashio::log\.\w+ .*BLINK_PROXY_TOKEN", run_sh),
        "the add-on never logs the token value",
    )
    check(
        "_async_handoff_token" in config_flow and "async_step_reauth" in config_flow,
        "the config flow pre-fills the shared token and can heal a rejected one",
    )


def test_versions_agree() -> None:
    print("\nthe version means the same thing everywhere")

    manifest = json.loads(
        (ROOT / "custom_components/blink_liveview_proxy/manifest.json").read_text()
    )["version"]
    addon = yaml.safe_load((ROOT / "addon/config.yaml").read_text())["version"]
    proxy = re.search(
        r'PROXY_VERSION = "([^"]+)"',
        (ROOT / "proxy/blink_proxy/constants.py").read_text(),
    )
    const_source = (ROOT / "custom_components/blink_liveview_proxy/const.py").read_text()
    minimum = re.search(r'MINIMUM_PROXY_VERSION = "([^"]+)"', const_source)
    environment = re.search(r'ENVIRONMENT_PROXY_VERSION = "([^"]+)"', const_source)

    check(proxy is not None, "the proxy declares a version")
    check(minimum is not None, "the integration declares the proxy version it needs")
    if not (proxy and minimum):
        return

    check(manifest == addon, f"manifest and add-on agree ({manifest} / {addon})")
    check(
        proxy.group(1) == manifest,
        f"the proxy reports the release version ({proxy.group(1)} / {manifest})",
    )
    # A minimum above the shipped version would put a repair notice in front of
    # everyone, including people running the matching proxy.
    def to_tuple(value: str) -> tuple[int, ...]:
        return tuple(int(part) for part in value.split("-", 1)[0].split("."))

    check(
        to_tuple(minimum.group(1)) <= to_tuple(manifest),
        f"the required proxy version is one that exists ({minimum.group(1)} <= {manifest})",
    )
    # The panel tells people which release started reporting /status
    # environment. Naming an unreleased one would ask them to install nothing.
    check(environment is not None, "the integration names the environment-reporting proxy")
    if environment:
        check(
            to_tuple(environment.group(1)) <= to_tuple(manifest),
            "the environment-reporting proxy version is one that exists "
            f"({environment.group(1)} <= {manifest})",
        )

    changelog = (ROOT / "CHANGELOG.md").read_text()
    check(f"## [{manifest}]" in changelog, f"the changelog has an entry for {manifest}")


def test_bootstrap_and_autoupdate() -> None:
    print("\none-line install, and the timer that reuses it")

    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        repo, opt = root / "repo", root / "opt"
        (opt / "blink_proxy").mkdir(parents=True)
        (opt / "blink_proxy" / "constants.py").write_text('PROXY_VERSION = "0.3.0"\n')
        repo.mkdir()
        git = ["git", "-C", str(repo)]
        subprocess.run(git + ["init", "-q", "."], check=True)
        subprocess.run(
            git + ["-c", "user.email=t@t", "-c", "user.name=t",
                   "commit", "-q", "--allow-empty", "-m", "x"],
            check=True,
        )
        # This mix catches three traps: alphabetical ordering, ignoring no-v
        # prerelease tags, and treating an rc as newer than its final release.
        for tag in ("v0.2.0", "v0.9.0", "v0.10.0", "0.11.0-rc.1"):
            subprocess.run(git + ["tag", tag], check=True)

        probe = f"""
        SRC_DIR={repo} OPT_DIR={opt}
        source {ROOT}/scripts/bootstrap.sh
        echo "newest_rc=$(newest_tag)"
        git -C {repo} tag v0.11.0
        echo "newest_stable=$(newest_tag)"
        echo "installed=$(installed_version)"
        should_install 'v0.3.0' '0.3.0' && echo same=install || echo same=skip
        should_install 'v0.10.0' '0.3.0' && echo newer=install || echo newer=skip
        should_install '0.11.0-rc.1' '0.10.0' && echo rc=install || echo rc=skip
        should_install 'v0.10.0' '0.11.0-rc.1' && echo downgrade=install || echo downgrade=skip
        should_install '0.11.0-rc.1' '0.11.0-rc.1' && echo same_rc=install || echo same_rc=skip
        should_install 'v0.3.0' '' && echo unknown=install || echo unknown=skip
        FORCE=1 should_install 'v0.3.0' '0.3.0' && echo forced=install || echo forced=skip
        """
        out = subprocess.run(
            ["bash", "-c", probe], check=True, capture_output=True, text=True
        ).stdout
        results = dict(
            line.split("=", 1) for line in out.strip().splitlines() if "=" in line
        )
        check(results.get("newest_rc") == "0.11.0-rc.1", "a prerelease tag is visible to the updater")
        check(results.get("newest_stable") == "v0.11.0", "a final release wins over its prerelease")
        check(results.get("installed") == "0.3.0", "the installed version is read from the deployed proxy")
        check(results.get("same") == "skip", "an up-to-date host does nothing")
        check(results.get("newer") == "install", "a newer tag installs")
        check(results.get("rc") == "install", "a newer prerelease installs")
        check(results.get("downgrade") == "skip", "an automatic check never downgrades")
        check(results.get("same_rc") == "skip", "a matching prerelease does nothing")
        check(results.get("unknown") == "install", "an install too old to report a version upgrades")
        check(results.get("forced") == "install", "FORCE reinstalls the same tag")

    bootstrap = (ROOT / "scripts/bootstrap.sh").read_text()
    check(
        "refusing to guess at main" in bootstrap,
        "a piped one-liner installs a tag, never whatever main holds",
    )

    # The advertised way to run this is `curl ... | sudo bash`, where bash reads
    # the script from stdin and BASH_SOURCE is unset. Under `set -u` that
    # aborted before main ran, and sourcing the file in a test could never see
    # it. So run it the way the documentation does.
    if os.geteuid() == 0:
        check(True, "piped run skipped: as root it would install for real")
    else:
        piped = subprocess.run(
            ["bash"], input=bootstrap, capture_output=True, text=True
        )
        output = piped.stdout + piped.stderr
        check("unbound variable" not in output, "piping the script into bash does not abort on an unset variable")
        check(
            "needs root" in output and piped.returncode == 1,
            "a piped run reaches the script's own checks",
        )
    check('if [ "$(id -u)" != "0" ]' in bootstrap, "it fails loudly rather than half-installing as a user")

    installer = (ROOT / "scripts/install-proxy.sh").read_text()
    check(
        'INSTALL_AUTOUPDATE:-0' in installer,
        "unattended updates are off unless asked for",
    )
    for fragment in (
        "/usr/local/sbin/blink-liveview-proxy-update.sh",
        "blink-liveview-proxy-update.timer",
        'printf \'SRC_DIR=%s\\n\'',
    ):
        check(fragment in installer, f"the auto-update install writes {fragment.split('/')[-1]}")

    unit = (ROOT / "systemd/blink-liveview-proxy-update.service").read_text()
    timer = (ROOT / "systemd/blink-liveview-proxy-update.timer").read_text()
    check(
        "/usr/local/sbin/blink-liveview-proxy-update.sh" in unit,
        "the unit runs the script the installer put in place",
    )
    check("EnvironmentFile=-/etc/blink-liveview-proxy/update.env" in unit, "the unit is told where the checkout is")
    check("Persistent=true" in timer and "RandomizedDelaySec" in timer, "the timer catches up, and does not stampede")


def test_standalone_image() -> None:
    print("\nstandalone Docker image")

    dockerfile = (ROOT / "Dockerfile").read_text()
    entrypoint = (ROOT / "docker/entrypoint.sh").read_text()
    compose = yaml.safe_load((ROOT / "docker-compose.example.yml").read_text())
    workflow = yaml.safe_load((ROOT / ".github/workflows/publish-image.yaml").read_text())

    check("COPY proxy/ ./" in dockerfile, "the image ships the canonical proxy copy")
    check("ffmpeg" in dockerfile, "ffmpeg is in the image, not assumed on the host")
    check('VOLUME ["/data"]' in dockerfile, "state is declared as a volume")
    check('ENTRYPOINT ["/entrypoint.sh"]' in dockerfile, "the entrypoint configures before serving")

    service = compose["services"]["blink-liveview-proxy"]
    image = service["image"].split(":")[0]
    published = [
        step["with"]["images"]
        for step in workflow["jobs"]["build"]["steps"]
        if step.get("id") == "meta"
    ]
    check(published and published[0] == image, f"compose points at the image CI publishes ({image})")
    check(image.islower(), "the image name is lowercase, which GHCR requires")
    check(
        any("/data" in v for v in service["volumes"]),
        "the example mounts the directory the image writes state into",
    )
    push = [
        step["with"]["push"]
        for step in workflow["jobs"]["build"]["steps"]
        if step.get("uses", "").startswith("docker/build-push-action")
    ]
    check(
        push and "startsWith(github.ref, 'refs/tags/')" in str(push[0]),
        "every push builds the image, but only a stable or prerelease tag publishes it",
    )

    # The entrypoint is what makes the image self-configuring; run it for real,
    # stopping just before it would exec the proxy.
    with tempfile.TemporaryDirectory() as tmp:
        data = pathlib.Path(tmp)
        probe = data / "probe.sh"
        probe.write_text(
            re.sub(
                r"^exec python3 .*$",
                'echo "token_exported=${BLINK_PROXY_TOKEN:+yes}"',
                entrypoint,
                flags=re.M,
            )
        )
        out = subprocess.run(
            ["bash", str(probe)],
            check=True,
            capture_output=True,
            text=True,
            env={
                "PATH": os.environ["PATH"],
                "DATA_DIR": str(data),
                "BLINK_PROXY_CONFIG": str(data / "config.json"),
            },
        ).stdout
        config = json.loads((data / "config.json").read_text())
        token = data / "proxy-token"
        check("token_exported=yes" in out, "a generated token reaches the proxy process")
        check(config["cameras"] == {} and config["host"] == "0.0.0.0", "the container config discovers cameras")
        check(
            all(str(data) in config[key] for key in ("auth_file", "hls_dir", "liveview_cache_dir", "clip_cache_dir")),
            "all state is written inside the volume, not the image layer",
        )
        check(oct(token.stat().st_mode)[-3:] == "600", "the generated token file is owner-only")
        check(token.read_text().strip() not in out, "the token value itself is never printed")

        # Second start: keep the identity the integration was given.
        first = token.read_text()
        subprocess.run(["bash", str(probe)], check=True, capture_output=True, text=True,
                       env={"PATH": os.environ["PATH"], "DATA_DIR": str(data),
                            "BLINK_PROXY_CONFIG": str(data / "config.json")})
        check(token.read_text() == first, "restarting the container keeps the same token")
        check(json.loads((data / "config.json").read_text()) == config, "an existing config is left alone")


def test_requirements_are_stated_once_and_true() -> None:
    print("\nthe stated requirements match the code")

    readme = (ROOT / "README.md").read_text()
    installer = (ROOT / "scripts/install-proxy.sh").read_text()
    hacs = json.loads((ROOT / "hacs.json").read_text())
    requirements = (ROOT / "proxy/requirements.txt").read_text()

    check("## Requirements" in readme, "the requirements are stated in one place")

    # The floor is documented, enforced, and the same number in both.
    # Bold can sit either side of the word, so compare on the plain text.
    documented = re.search(r"Python (\d+\.\d+)\+", readme.replace("**", ""))
    enforced = re.search(r"sys\.version_info >= \((\d+), (\d+)\)", installer)
    check(documented is not None, "the README names a Python version")
    check(enforced is not None, "the installer enforces a Python version")
    if documented and enforced:
        check(
            documented.group(1) == f"{enforced.group(1)}.{enforced.group(2)}",
            f"documented and enforced Python agree ({documented.group(1)})",
        )

    # HACS refuses to install below this; the README should not promise less.
    check(
        hacs["homeassistant"].rsplit(".", 1)[0] in readme or hacs["homeassistant"] in readme,
        f"the README names the Home Assistant version HACS enforces ({hacs['homeassistant']})",
    )
    # The panel reports the same floor; two numbers here means one of them lies.
    declared_floor = re.search(
        r'MINIMUM_HA_VERSION = "([^"]+)"',
        (ROOT / "custom_components/blink_liveview_proxy/const.py").read_text(),
    )
    check(
        declared_floor is not None and declared_floor.group(1) == hacs["homeassistant"],
        f"const.py and hacs.json agree on the floor ({hacs['homeassistant']})",
    )
    # OptionsFlow.config_entry, which the options flow reads, only exists from
    # 2024.11. A lower floor installs fine and breaks the moment Options opens.
    check(
        tuple(int(part) for part in hacs["homeassistant"].split(".")) >= (2024, 11, 0),
        "the floor is at least 2024.11.0, where OptionsFlow.config_entry appeared",
    )

    # blinkpy is pinned exactly, on purpose: 0.25.5 reads Blink's 2FA challenge
    # as a failed login. A range here would let that back in.
    # Comments now explain each dependency; they must not confuse the parsers
    # that read this file (pip in CI, pip in the installer, the add-on build).
    entries = [
        line.strip()
        for line in requirements.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    check(
        {entry.split("=")[0].split(">")[0] for entry in entries}
        == {"aiohttp", "blinkpy", "certifi"},
        "the requirements file still declares exactly the three dependencies",
    )
    check(
        all("why" not in entry.lower() for entry in entries),
        "the explanations are comments, not requirement lines",
    )

    pin = re.search(r"^blinkpy==(\S+)$", requirements, re.M)
    check(pin is not None, "blinkpy is pinned to an exact version, not a range")
    if pin:
        check(pin.group(1) in readme, f"the README names the pin it relies on ({pin.group(1)})")
        # The dashboard compares the proxy's blinkpy against this copy of the
        # pin. If the two drift, a correct install is told to fix itself.
        declared = re.search(
            r'REQUIRED_BLINKPY_VERSION = "([^"]+)"',
            (ROOT / "custom_components/blink_liveview_proxy/const.py").read_text(),
        )
        check(declared is not None, "the integration names the blinkpy it expects")
        check(
            bool(declared) and declared.group(1) == pin.group(1),
            f"the dashboard checks for the version actually pinned ({pin.group(1)})",
        )

    for card in ("button-card", "auto-entities"):
        check(card in readme, f"the dashboard dependency {card} is named")
    for tool in ("ffmpeg", "git"):
        check(tool in readme, f"the host dependency {tool} is named")


def test_asset_paths_avoid_the_service_worker() -> None:
    """Nothing this project serves may sit under a path Home Assistant caches.

    Home Assistant's service worker registers a CacheFirst route for
    /(static|frontend_latest|frontend_es5)/.+ before its /api rule, and Workbox
    matches a RegExp anywhere in a same-origin URL rather than only at the
    start. A path with any of those segments in it was therefore served out of
    Cache Storage forever, and the route sets ignoreSearch, so a ?v= buster is
    stripped from the key and changes nothing. Only the path can move.

    It bit HTTPS alone, because a service worker needs a secure context, which
    is why it survived so long: over plain HTTP everything looked fine.
    """
    print("\nserved paths steer clear of the service worker's cache")

    captured = {"static", "frontend_latest", "frontend_es5"}
    const_source = (ROOT / "custom_components/blink_liveview_proxy/const.py").read_text()
    base = re.search(r'ASSET_URL_BASE = "([^"]+)"', const_source)
    legacy = re.search(r'LEGACY_ASSET_URL_BASE = "([^"]+)"', const_source)
    check(base is not None, "the asset base is declared in one place")
    check(legacy is not None, "the superseded path is named, so it stays served")
    if not (base and legacy):
        return

    check(
        not (set(base.group(1).split("/")) & captured),
        f"the asset base has no cached segment ({base.group(1)})",
    )
    # The legacy path is meant to contain one. It exists to keep answering.
    check(
        bool(set(legacy.group(1).split("/")) & captured),
        "the superseded path is the cached one, kept only for compatibility",
    )

    # The panel repeats the base in JavaScript, where it cannot import it.
    panel = (ROOT / "custom_components/blink_liveview_proxy/frontend"
             / "blink-proxy-auth-panel.js").read_text()
    urls = set(re.findall(r"/api/blink_liveview_proxy/[a-z_]+/", panel))
    check(
        urls == {f"{base.group(1)}/"},
        f"the panel's own URLs all use that base ({sorted(urls)})",
    )

    # Code and config must have moved: an example or generator still emitting
    # the old URL would hand new installs the cached path on purpose. Prose is
    # not scanned - the documentation has to be able to say what moved and
    # that the old path still answers.
    allowed = {
        "custom_components/blink_liveview_proxy/const.py",
        "tests/test_assets.py",
        "tests/test_frontend_resource.py",
        "tests/test_prerequisites.py",
    }
    stale = [
        path.relative_to(ROOT).as_posix()
        for path in tracked("*.py", "*.js", "*.yaml", "*.yml")
        if legacy.group(1) in path.read_text()
        and path.relative_to(ROOT).as_posix() not in allowed
    ]
    for entry in stale:
        print(f"        {entry}")
    check(not stale, f"nothing else still points at the superseded path ({len(stale)})")


def test_liveview_safe_areas_do_not_pad_the_player() -> None:
    """The player must paint behind iOS's notch and home-indicator insets."""
    print("\nlive-view safe areas belong to controls, not the player shell")
    source = (
        ROOT
        / "custom_components/blink_liveview_proxy/frontend/blink-liveview-dialog.js"
    ).read_text()

    for edge in ("top", "right", "bottom", "left"):
        check(
            f"padding-{edge}: env(safe-area-inset-{edge}" not in source,
            f"the outer shell has no {edge} safe-area padding",
        )
    check(
        "top: calc(10px + env(safe-area-inset-top, 0px));" in source,
        "the close button stays below the top safe area",
    )
    check(
        "left: calc(10px + env(safe-area-inset-left, 0px));" in source,
        "the close button stays clear of a landscape notch",
    )
    check(
        "background: #05070a;" in source,
        "the shell cannot inherit Home Assistant's white card background",
    )
    check(
        'iframe.contentDocument?.querySelector("video")' in source,
        "the dialog measures the same-origin player's rendered video",
    )
    check(
        "videoLeft - close.offsetWidth - gap" in source,
        "the landscape close button sits just left of the video",
    )
    check(
        "root.clientWidth <= root.clientHeight" in source,
        "portrait keeps the safe-area CSS position",
    )

    player = (
        ROOT / "custom_components/blink_liveview_proxy/views.py"
    ).read_text()
    check(
        'liveActions.classList.add("bottom-gutter")' in player,
        "portrait can put live actions in the gutter below the video",
    )
    check(
        "window.innerHeight - videoRect.bottom" in player,
        "bottom placement uses the rendered video edge",
    )
    check(
        "roomBelow >= liveActions.offsetHeight + 80" in player,
        "buttons move only when the gutter clears the home indicator",
    )


def test_sound_is_reachable_on_the_player() -> None:
    print("\nlive view starts muted, and says how to change that")

    player = (ROOT / "custom_components/blink_liveview_proxy/views.py").read_text()

    check(
        '<video id="video" muted playsinline autoplay' in player,
        "the stream still starts muted, which is the only way it starts at all",
    )
    check(
        '<button id="sound"' in player and '>Unmute</button>' in player,
        "the control bar carries a sound button, not just the native one",
    )
    check(
        'sound.textContent = video.muted ? "Unmute" : "Mute"' in player,
        "the button says what tapping it does",
    )
    check(
        player.count("await startPlayback();") == 2,
        "both the HLS and the MPEG-TS path start through the same helper",
    )
    check(
        "if (soundWanted()) video.muted = false;" in player,
        "someone who unmuted last time is not asked again",
    )
    check(
        "restoringMute = true;" in player and "video.muted = true;" in player,
        "a refused unmuted start falls back to the muted one, not to a still frame",
    )
    check(
        player.count("statusText.textContent = \"Tap play to start live view\";") == 2,
        "the fallback still ends in the old message when even muted is refused",
    )
    check(
        "if (!restoringMute) rememberSound(!video.muted);" in player,
        "that fallback is not remembered as a preference",
    )
    check(
        "} catch (err) {" in player.split("function soundWanted()")[1][:400],
        "storage that throws leaves the player working, without sound memory",
    )


def test_proxy_status_codes_survive_the_integration() -> None:
    print("\nwhat the proxy answered is what the browser sees")

    views = (ROOT / "custom_components/blink_liveview_proxy/views.py").read_text()
    mapping = views[views.index("async def _open_proxy_response") :]
    mapping = mapping[: mapping.index("async def _proxy_stream")]

    check(
        "response.status == 416" in mapping,
        "an unsatisfiable range is answered as one",
    )
    check(
        "HTTPRequestRangeNotSatisfiable" in mapping,
        "416 does not become a gateway error",
    )
    check(
        mapping.index("response.status == 416") < mapping.index("response.status >= 400"),
        "the 416 branch is reached before the catch-all",
    )
    check(
        'headers={"Content-Range": content_range} if content_range else None' in mapping,
        "the header that says what range would have worked is kept",
    )


def test_addon_reaches_the_options_that_matter() -> None:
    print("\nthe add-on can be configured for what its users hit")

    config = yaml.safe_load((ROOT / "addon/config.yaml").read_text())
    build = (ROOT / "addon/build_config.py").read_text()

    excluded = config.get("backup_exclude") or []
    for name in ("clips/", "liveviews/"):
        check(name in excluded, f"{name} stays out of every snapshot")

    for key in (
        "ptt_disabled_product_types",
        "ptt_disabled_camera_types",
        "ptt_force_enabled_slugs",
    ):
        check(key in config["schema"], f"{key} is settable from the add-on UI")
        check(
            config["options"].get(key) == [],
            f"{key} starts empty, meaning 'keep the proxy default'",
        )
        check(key in build, f"{key} reaches the generated proxy config")

    check(
        "value = options.get(key) or []" in build and "if value:" in build,
        "an empty list is left out, so it cannot overwrite a proxy default",
    )

    defaults = (ROOT / "proxy/blink_proxy/constants.py").read_text()
    for key in (
        "ptt_disabled_product_types",
        "ptt_disabled_camera_types",
        "ptt_force_enabled_slugs",
    ):
        check(
            f'"{key}"' in defaults,
            f"{key} is a key the proxy actually reads ({key})",
        )


def test_a_tag_cannot_ship_the_wrong_version() -> None:
    print("\nthe tag and the build agree, or nothing publishes")

    workflow = (ROOT / ".github/workflows/publish-image.yaml").read_text()
    build = yaml.safe_load(workflow)["jobs"]["build"]["steps"]
    names = [step.get("name") for step in build]

    check(
        "The tag and the shipped version must agree" in names,
        "a tag build checks itself against the tag",
    )
    guard = names.index("The tag and the shipped version must agree")
    check(
        guard < names.index("Build (and push on a tag)"),
        "the check runs before anything is built or pushed",
    )
    check(
        build[guard].get("if", "").startswith("startsWith(github.ref, 'refs/tags/')"),
        "it only applies where there is a tag to compare",
    )
    for name in (
        "manifest.json",
        "addon/config.yaml",
        "proxy/blink_proxy/constants.py",
    ):
        check(name in build[guard]["run"], f"{name} is compared to the tag")


def test_cloud_clips_cost_nothing_until_asked() -> None:
    print("\ncloud clips are listed, and fetched only on purpose")

    views = (ROOT / "custom_components/blink_liveview_proxy/views.py").read_text()

    check(
        'source: "both"' in views,
        "the viewer asks for both inventories, not just the Sync Module",
    )
    check(
        'if (clip.source === "cloud" && !cloudThumbnailReady(clip))' in views,
        "a cloud clip draws a placeholder rather than fetching itself",
    )
    check(
        "cloudThumbnailsOn || fetchedClips.has(clip.id)" in views,
        "a cloud thumbnail is drawn once its clip is on disk anyway",
    )
    check(
        "window.confirm(warning)" in views and "downloaded from Blink first" in views,
        "the bulk button says what it is about to download, and can be refused",
    )
    check(
        'fetchedClips.add(clip.id);' in views and "upgradeThumbnail(clip)" in views,
        "playing a cloud clip fills its tile in, at no further cost",
    )
    check(
        views.index("function loadCloudThumbnails") < views.index("cloudThumbnailsOn = true"),
        "nothing turns cloud thumbnails on except that button",
    )

    # The source has to survive the integration, or a cloud clip 404s on the
    # way to its own bytes.
    check(
        "def _clip_query(request: web.Request, *, allow_both: bool)" in views,
        "one place decides which clip sources a request may name",
    )
    check(
        'permitted = {"local", "cloud", "both"} if allow_both else {"local", "cloud"}' in views,
        "the source is an enum, and only the listing may say both",
    )
    check(
        views.count("_clip_query(request, allow_both=False)") == 2,
        "the download and thumbnail routes carry a source too",
    )
    check(
        'query["source"] = "local"' not in views,
        "no route pins itself to local any more",
    )

    docs = (ROOT / "docs/CONFIGURATION.md").read_text()
    check(
        "only for an account with a Blink subscription" in docs,
        "the docs say what a cloud clip costs and who has one",
    )


def test_push_to_talk_defaults_to_offered() -> None:
    print("\npush-to-talk is not hidden from a family that has it")

    defaults = (ROOT / "proxy/blink_proxy/constants.py").read_text()
    for key in ("ptt_disabled_camera_types", "ptt_disabled_product_types"):
        check(
            f'"{key}": [],' in defaults,
            f"{key} ships empty, so no family is refused by default",
        )
    check(
        '"ptt_force_enabled_slugs": [],' in defaults,
        "the per-camera override is still there for a family that is listed",
    )

    docs = (ROOT / "docs/CONFIGURATION.md").read_text()
    check(
        '"ptt_disabled_product_types": []' in docs,
        "the documented default is the shipped one",
    )


def test_clips_toolbar_clears_the_status_bar() -> None:
    """The clip viewer's toolbar is the one thing on that page under the notch."""
    print("\nclips toolbar sits below the phone's status bar")
    views = (ROOT / "custom_components/blink_liveview_proxy/views.py").read_text()
    toolbar = re.search(r"\n\.toolbar\{([^}]*)\}", views)
    check(toolbar is not None, "the clips toolbar rule is where it was")
    if toolbar is None:
        return
    check(
        "padding:calc(10px + env(safe-area-inset-top,0px)) 14px 10px" in toolbar.group(1),
        "its top padding grows by the status bar inset",
    )


def main() -> int:
    for test in (
        test_yaml_parses,
        test_json_parses,
        test_button_card_styles,
        test_hacs_json,
        test_manifest,
        test_translations_shipped,
        test_proxy_pill_keeps_its_mark,
        test_generator_shapes,
        test_proxy_copies_match,
        test_install_token,
        test_install_writes_config,
        test_addon_token_handoff,
        test_versions_agree,
        test_bootstrap_and_autoupdate,
        test_standalone_image,
        test_requirements_are_stated_once_and_true,
        test_asset_paths_avoid_the_service_worker,
        test_liveview_safe_areas_do_not_pad_the_player,
        test_sound_is_reachable_on_the_player,
        test_proxy_status_codes_survive_the_integration,
        test_addon_reaches_the_options_that_matter,
        test_a_tag_cannot_ship_the_wrong_version,
        test_cloud_clips_cost_nothing_until_asked,
        test_push_to_talk_defaults_to_offered,
        test_clips_toolbar_clears_the_status_bar,
    ):
        test()

    print(f"\n{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
    if FAILURES:
        print("\nfailed:")
        for name in FAILURES:
            print(f"  {name}")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
