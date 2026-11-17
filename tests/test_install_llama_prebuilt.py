"""#1356: on Apple Silicon tlwu_install_llama first takes the project-built llama-cpp-python wheel (release asset,
SHA256-checked) and runs the old index-wheel / sdist path on any failure. Runs the real shell functions against a
stub python that records its argv and a local folder served through file:// in place of the GitHub release."""
from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
COMMON_SH = ROOT / "scripts" / "tlwu_common.sh"
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None or shutil.which("curl") is None, reason="bash and curl needed")

VERSION = "9.8.7"  # not the real pin: the version must come from the requirements file the function is given
PLATFORM = "macosx_arm64"
WHEEL = f"llama_cpp_python-{VERSION}-py3-none-macosx_11_0_arm64.whl"
SUMS = "SHA256SUMS.txt"
STUB = """#!/bin/bash
echo "$@" >> "$STUB_LOG"
case "$*" in
  *"import llama_cpp"*) exit "${IMPORT_RC:-0}";;
  *--no-binary*) exit "${SDIST_RC:-0}";;
  *"install -r"*) exit "${INDEX_RC:-0}";;
  *uninstall*) exit 0;;
  *.whl) cp "${@: -1}" "$STUB_LOG.wheel"; exit "${PREBUILT_RC:-0}";;
esac
"""


def _release(tmp_path, wheel=WHEEL, content=b"wheel bytes", sha=None, version=VERSION):
    """A fake release folder <base>/<tag>/ with the wheel and its checksum file; returns the base URL."""
    rel = tmp_path / "rel" / f"wheels-llama-cpp-python-{version}"
    rel.mkdir(parents=True)
    if content is not None:
        (rel / Path(wheel).name).write_bytes(content)
    digest = sha or hashlib.sha256(content or b"").hexdigest()
    (rel / SUMS).write_text(f"{digest}  {wheel}\n")
    return (tmp_path / "rel").as_uri()


def _run(tmp_path, base, func="tlwu_install_llama", platform=PLATFORM, **env):
    py = tmp_path / "py"
    py.write_text(STUB)
    py.chmod(0o755)
    req = tmp_path / "req.txt"
    req.write_text(f"--extra-index-url https://example.invalid/whl/cpu\nllama-cpp-python=={VERSION}\n")
    log = tmp_path / "log"
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    full_env = {"PATH": "/usr/bin:/bin", "STUB_LOG": str(log), "TMPDIR": str(tmpdir), "TLWU_LLAMA_WHEEL_BASE": base,
                "TLWU_LLAMA_WHEEL_PLATFORM": platform, **env}
    r = subprocess.run([BASH, "-c", f'. "{COMMON_SH}"; {func} "{py}" "{req}"'], env=full_env, capture_output=True,
                       text=True)
    calls = log.read_text().splitlines() if log.exists() else []
    installed = Path(str(log) + ".wheel")
    assert not list(tmpdir.iterdir()), "the download folder is removed on every path"
    return r, calls, installed.read_bytes() if installed.exists() else None


def _fell_back(calls):
    """The old path ran: the index install, then the sdist build of the pinned version."""
    return [c for c in calls if "install -r" in c or "--no-binary" in c]


def test_prebuilt_wheel_is_verified_and_installed_without_a_build(tmp_path):
    r, calls, installed = _run(tmp_path, _release(tmp_path, content=b"good wheel"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert installed == b"good wheel"
    assert calls[0].startswith("-m pip install ") and calls[0].endswith("/" + WHEEL)
    assert calls[1] == "-c import llama_cpp" and len(calls) == 2  # no index install, no sdist build
    assert f"prebuilt wheel installed: {WHEEL} (SHA256 ok)" in r.stdout


def test_checksum_mismatch_never_installs_the_wheel_and_builds_from_source(tmp_path):
    base = _release(tmp_path, content=b"tampered wheel", sha=hashlib.sha256(b"the published wheel").hexdigest())
    r, calls, installed = _run(tmp_path, base, INDEX_RC="1")
    assert r.returncode == 0
    assert installed is None and not any(c.endswith(".whl") for c in calls)
    assert "SHA256 mismatch" in r.stdout
    assert len(_fell_back(calls)) == 2 and f"--no-binary llama-cpp-python llama-cpp-python=={VERSION}" in calls[-1]


def test_no_release_falls_back(tmp_path):
    (tmp_path / "rel").mkdir()
    r, calls, installed = _run(tmp_path, (tmp_path / "rel").as_uri(), INDEX_RC="1")
    assert r.returncode == 0 and installed is None
    assert "no checksum file" in r.stdout and len(_fell_back(calls)) == 2 and len(calls) == 2


def test_wheel_missing_from_the_release_falls_back(tmp_path):
    r, calls, installed = _run(tmp_path, _release(tmp_path, content=None), INDEX_RC="1")
    assert r.returncode == 0 and installed is None
    assert "download of" in r.stdout and len(_fell_back(calls)) == 2


def test_pip_refusing_the_wheel_falls_back(tmp_path):
    # e.g. a macOS older than the wheel's platform tag
    r, calls, _ = _run(tmp_path, _release(tmp_path), PREBUILT_RC="1", INDEX_RC="1")
    assert r.returncode == 0 and calls[0].endswith(".whl") and len(_fell_back(calls)) == 2
    assert not any("import llama_cpp" in c for c in calls)


def test_wheel_that_does_not_import_is_removed_then_built_from_source(tmp_path):
    r, calls, _ = _run(tmp_path, _release(tmp_path), IMPORT_RC="1", INDEX_RC="1")
    assert r.returncode == 0
    assert calls[2] == "-m pip uninstall -y llama-cpp-python" and "--no-binary" in calls[-1]


def test_every_path_failing_returns_nonzero(tmp_path):
    base = _release(tmp_path, sha="0" * 64)
    r, _, _ = _run(tmp_path, base, INDEX_RC="1", SDIST_RC="5")
    assert r.returncode == 5


@pytest.mark.parametrize("line", [
    "{sha}  ../" + WHEEL,                                               # path outside the download folder
    "{sha}  sub/" + WHEEL,
    "{sha}  " + WHEEL.replace(VERSION, "0.0.1"),                        # another version than the pin
    "{sha}  " + WHEEL.replace("arm64", "x86_64"),                       # another platform
    "nothex  " + WHEEL,
    "{sha}",
])
def test_checksum_file_without_a_usable_line_falls_back(tmp_path, line):
    base = _release(tmp_path)
    sha = hashlib.sha256(b"wheel bytes").hexdigest()
    (tmp_path / "rel" / f"wheels-llama-cpp-python-{VERSION}" / SUMS).write_text(line.format(sha=sha) + "\n")
    r, calls, installed = _run(tmp_path, base, func="tlwu_install_llama_prebuilt")
    assert r.returncode == 1 and installed is None and calls == []
    assert "lists no" in r.stdout


def test_this_platforms_line_is_picked_from_several(tmp_path):
    base = _release(tmp_path, content=b"arm wheel")
    sums = tmp_path / "rel" / f"wheels-llama-cpp-python-{VERSION}" / SUMS
    other = WHEEL.replace("arm64", "x86_64")
    sums.write_text(f"{'1' * 64}  {other}\n{hashlib.sha256(b'arm wheel').hexdigest()} *{WHEEL}\n")  # `*` = binary mode
    r, _, installed = _run(tmp_path, base, func="tlwu_install_llama_prebuilt")
    assert r.returncode == 0 and installed == b"arm wheel"


def test_other_platforms_do_not_download_anything(tmp_path):
    # Linux / Intel Mac: straight to the index wheel, as before
    r, calls, installed = _run(tmp_path, _release(tmp_path), platform="")
    assert r.returncode == 0 and installed is None and len(calls) == 1 and "install -r" in calls[0]
    assert "prebuilt" not in r.stdout


def test_opt_out_env_skips_the_prebuilt_wheel(tmp_path):
    _, calls, installed = _run(tmp_path, _release(tmp_path), TLWU_LLAMA_NO_PREBUILT="1")
    assert installed is None and len(calls) == 1 and "install -r" in calls[0]


def test_platform_is_detected_from_uname(tmp_path):
    def platform(system, machine):
        bindir = tmp_path / f"bin-{system}-{machine}"
        bindir.mkdir()
        uname = bindir / "uname"
        uname.write_text(f'#!/bin/bash\ncase "$1" in -s) echo {system};; -m) echo {machine};; esac\n')
        uname.chmod(0o755)
        r = subprocess.run([BASH, "-c", f'. "{COMMON_SH}"; tlwu_llama_wheel_platform'],
                           env={"PATH": f"{bindir}:/usr/bin:/bin"}, capture_output=True, text=True)
        return r.stdout
    assert platform("Darwin", "arm64") == PLATFORM
    assert platform("Darwin", "x86_64") == "" and platform("Linux", "aarch64") == ""


def test_release_tag_and_url_follow_the_pin_in_the_requirements_file(tmp_path):
    # one place for the version: a different pin moves the tag, the URL and the wheel name with it
    req = tmp_path / "r.txt"
    req.write_text("# comment\n--extra-index-url https://example.invalid\nllama-cpp-python==1.2.3 \n")
    r = subprocess.run([BASH, "-c", f'. "{COMMON_SH}"; tlwu_llama_pin "{req}"; echo; tlwu_llama_version "{req}"; echo; '
                                    f'tlwu_llama_wheel_tag "{req}"; echo; tlwu_llama_wheel_glob 1.2.3 {PLATFORM}; echo; '
                                    f'tlwu_llama_wheel_tag /nonexistent; echo "rc=$?"'],
                       env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True)
    assert r.stdout.splitlines() == ["llama-cpp-python==1.2.3", "1.2.3", "wheels-llama-cpp-python-1.2.3",
                                     "llama_cpp_python-1.2.3-*-macosx_*_arm64.whl", "rc=1"]


def test_real_pin_gives_the_tag_the_wheel_workflow_publishes():
    yaml = pytest.importorskip("yaml")
    wf = yaml.safe_load((ROOT / ".github/workflows/llama-wheel-mac.yml").read_text(encoding="utf-8"))
    req = wf["env"]["REQ"]
    r = subprocess.run([BASH, "-c", f'. "{COMMON_SH}"; tlwu_llama_wheel_tag "{ROOT / req}"'],
                       env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True)
    assert r.stdout.startswith("wheels-llama-cpp-python-") and r.stdout[len("wheels-llama-cpp-python-")].isdigit()
    on = wf.get("on", wf.get(True))
    assert req in on["push"]["paths"] and "workflow_dispatch" in on  # rebuilt when the pin changes
    assert wf["permissions"] == {} and wf["jobs"]["build"]["permissions"] == {"contents": "read"}  # build cannot publish
    assert wf["jobs"]["publish"]["permissions"] == {"contents": "write"}
