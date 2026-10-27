"""Execute the real Bash installer with fake tools; no apt/network/system writes."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install_tesseract_render.sh"
BASH = shutil.which("bash") or (r"C:\Program Files\Git\bin\bash.exe" if os.name == "nt" else None)

FAKE_TOOL = r'''#!/usr/bin/env bash
set -euo pipefail
tool="${0##*/}"
root="$(cd "$FAKE_BUILD_ROOT" && pwd)"
printf '%s' "$tool" >> "$FAKE_LOG"
printf '\t%s' "$@" >> "$FAKE_LOG"
printf '\n' >> "$FAKE_LOG"
if [ "$tool" = dpkg-deb ]; then
  [ "$1" = -x ] && [ -f "$2" ]
  install_root="$3"
  mkdir -p "$install_root/usr/bin" "$install_root/usr/share/tesseract-ocr/5/tessdata"
  cat > "$install_root/usr/bin/tesseract" <<'TESSERACT'
#!/usr/bin/env bash
if [ "$1" = --list-langs ]; then
  printf 'List of available languages (2):\neng\n'
  if [ "${FAKE_MISSING_JPN:-0}" != 1 ]; then printf 'jpn\n'; fi
else
  printf 'tesseract fictional-version\n'
fi
TESSERACT
  chmod +x "$install_root/usr/bin/tesseract"
  exit 0
fi
lists=''
cache=''
archives=''
pkgcache=''
srcpkgcache=''
log=''
error_mode=''
while [ "${1:-}" = -o ]; do
  case "$2" in
    Dir::State::Lists=*) lists="${2#*=}" ;;
    Dir::Cache=*) cache="${2#*=}" ;;
    Dir::Cache::archives=*) archives="${2#*=}" ;;
    Dir::Cache::pkgcache=*) pkgcache="${2#*=}" ;;
    Dir::Cache::srcpkgcache=*) srcpkgcache="${2#*=}" ;;
    Dir::Log=*) log="${2#*=}" ;;
    APT::Update::Error-Mode=*) error_mode="${2#*=}" ;;
    *) echo 'unexpected or unsafe apt option' >&2; exit 91 ;;
  esac
  shift 2
done
[ "$lists" = "$root/.render/apt-state/lists" ]
[ "$cache" = "$root/.render/apt-cache" ]
[ "$archives" = "$cache/archives" ]
[ "$pkgcache" = "$cache/pkgcache.bin" ]
[ "$srcpkgcache" = "$cache/srcpkgcache.bin" ]
[ "$log" = "$root/.render/apt-log" ]
[ "$error_mode" = any ]
[ -d "$lists/partial" ] && [ -d "$archives/partial" ] && [ -d "$log" ]
case "$tool:$1" in
  apt-get:update)
    if [ "${FAKE_FAIL_UPDATE:-0}" = 1 ]; then exit 100; fi
    printf 'fictional signed index\n' > "$lists/fixture-index"
    ;;
  apt-cache:depends)
    [ -f "$lists/fixture-index" ]
    if [ "${FAKE_FAIL_DEPENDS:-0}" = 1 ]; then exit 100; fi
    printf 'tesseract-ocr\n  Depends: libfixture1\n  Depends: <virtual-fixture>\n'
    ;;
  apt-get:download)
    [ -f "$lists/fixture-index" ] && [ "$PWD" = "$cache" ]
    if [ "$2" = virtual-fixture ]; then exit 100; fi
    printf 'fictional deb\n' > "$2.deb"
    ;;
  *) echo 'system installation is forbidden in this fixture' >&2; exit 92 ;;
esac
'''


@unittest.skipUnless(BASH and Path(BASH).is_file(), "Bash is required for the fake installer test")
class TesseractRenderInstallerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="kaika apt fixture ")
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name).resolve()
        self.fake_bin = self.work / "fake-bin"; self.fake_bin.mkdir()
        self.script = self.work / "install.sh"
        self.script.write_text(INSTALLER.read_text(encoding="utf-8-sig"), encoding="utf-8", newline="\n")
        for name in ("apt-get", "apt-cache", "dpkg-deb"):
            tool = self.fake_bin / name
            tool.write_text(FAKE_TOOL, encoding="utf-8", newline="\n"); tool.chmod(0o755)
        bash_dir = Path(BASH).parent
        self.env = os.environ.copy()
        self.env.pop("RENDER_TESSERACT_ROOT", None)
        self.env.pop("TESSDATA_PREFIX", None)
        self.env.update(PATH=os.pathsep.join((str(self.fake_bin), str(bash_dir), str(bash_dir.parent / "usr" / "bin"), self.env.get("PATH", ""))),
                        FAKE_BUILD_ROOT=self.work.as_posix(), FAKE_LOG=(self.work / "calls.txt").as_posix())

    def run_installer(self, **variables):
        return subprocess.run([BASH, "--noprofile", "--norc", self.script.as_posix()], cwd=self.work,
                              env={**self.env, **variables}, capture_output=True, text=True, timeout=30)

    def calls(self):
        log = self.work / "calls.txt"
        return log.read_text(encoding="utf-8").splitlines() if log.exists() else []

    def test_bash_syntax(self):
        result = subprocess.run([BASH, "-n", self.script.as_posix()], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_update_dependency_resolution_and_download_share_private_state(self):
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.calls()
        apt_calls = [line for line in calls if line.startswith(("apt-get\t", "apt-cache\t"))]
        self.assertTrue(any(line.endswith("\tupdate") for line in apt_calls))
        self.assertTrue(any("\tdepends\t" in line for line in apt_calls))
        for name in ("tesseract-ocr", "tesseract-ocr-eng", "tesseract-ocr-jpn", "libfixture1"):
            self.assertTrue(any(line.endswith("\tdownload\t" + name) for line in apt_calls), name)
        self.assertFalse(any("\tinstall\t" in line for line in apt_calls))
        for line in apt_calls:
            self.assertIn("Dir::State::Lists=", line)
            self.assertIn("Dir::Cache::pkgcache=", line)
            self.assertNotIn("allow-unauthenticated", line)
            self.assertNotIn("AllowInsecure", line)
        self.assertIn("tesseract fictional-version", result.stdout)
        self.assertTrue((self.work / ".render" / "tesseract" / "usr" / "bin" / "tesseract").is_file())

    def test_failed_index_update_does_not_resolve_or_download_packages(self):
        result = self.run_installer(FAKE_FAIL_UPDATE="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.calls()), 1)
        self.assertTrue(self.calls()[0].endswith("\tupdate"))

    def test_failed_dependency_resolution_does_not_download_partial_packages(self):
        result = self.run_installer(FAKE_FAIL_DEPENDS="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.calls()), 2)
        self.assertFalse(any("\tdownload\t" in call for call in self.calls()))

    def test_existing_english_and_japanese_tesseract_returns_without_apt(self):
        existing = self.fake_bin / "tesseract"
        existing.write_text("#!/usr/bin/env bash\nif [ \"$1\" = --list-langs ]; then printf 'eng\\njpn\\n'; else printf 'existing fixture\\n'; fi\n",
                            encoding="utf-8", newline="\n")
        existing.chmod(0o755)
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("tesseract already available", result.stdout)
        self.assertEqual(self.calls(), [])

    def test_incomplete_language_install_is_not_reported_as_ready(self):
        result = self.run_installer(FAKE_MISSING_JPN="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("required languages (eng, jpn) were not installed", result.stderr)


if __name__ == "__main__":
    unittest.main()
