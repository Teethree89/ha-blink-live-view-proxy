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
    minimum = re.search(
        r'MINIMUM_PROXY_VERSION = "([^"]+)"',
        (ROOT / "custom_components/blink_liveview_proxy/const.py").read_text(),
    )

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
        return tuple(int(part) for part in value.split("."))

    check(
        to_tuple(minimum.group(1)) <= to_tuple(manifest),
        f"the required proxy version is one that exists ({minimum.group(1)} <= {manifest})",
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
        # v0.10.0 last, so an alphabetical sort would pick v0.9.0 and quietly
        # stop upgrading anyone after the ninth release.
        for tag in ("v0.2.0", "v0.9.0", "v0.10.0"):
            subprocess.run(git + ["tag", tag], check=True)

        probe = f"""
        SRC_DIR={repo} OPT_DIR={opt}
        source {ROOT}/scripts/bootstrap.sh
        echo "newest=$(newest_tag)"
        echo "installed=$(installed_version)"
        should_install 'v0.3.0' '0.3.0' && echo same=install || echo same=skip
        should_install 'v0.10.0' '0.3.0' && echo newer=install || echo newer=skip
        should_install 'v0.3.0' '' && echo unknown=install || echo unknown=skip
        FORCE=1 should_install 'v0.3.0' '0.3.0' && echo forced=install || echo forced=skip
        """
        out = subprocess.run(
            ["bash", "-c", probe], check=True, capture_output=True, text=True
        ).stdout
        results = dict(
            line.split("=", 1) for line in out.strip().splitlines() if "=" in line
        )
        check(results.get("newest") == "v0.10.0", "the newest tag is chosen by version, not alphabetically")
        check(results.get("installed") == "0.3.0", "the installed version is read from the deployed proxy")
        check(results.get("same") == "skip", "an up-to-date host does nothing")
        check(results.get("newer") == "install", "a newer tag installs")
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
        push and "startsWith(github.ref, 'refs/tags/v')" in str(push[0]),
        "every push builds the image, but only a tag publishes it",
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
            all(str(data) in config[key] for key in ("auth_file", "hls_dir", "liveview_cache_dir")),
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

    for card in ("button-card", "auto-entities"):
        check(card in readme, f"the dashboard dependency {card} is named")
    for tool in ("ffmpeg", "git"):
        check(tool in readme, f"the host dependency {tool} is named")


def main() -> int:
    for test in (
        test_yaml_parses,
        test_json_parses,
        test_button_card_styles,
        test_hacs_json,
        test_manifest,
        test_generator_shapes,
        test_proxy_copies_match,
        test_install_token,
        test_install_writes_config,
        test_addon_token_handoff,
        test_versions_agree,
        test_bootstrap_and_autoupdate,
        test_standalone_image,
        test_requirements_are_stated_once_and_true,
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
