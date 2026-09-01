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
        (l for l in unit.splitlines() if l.startswith("EnvironmentFile=")), ""
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
