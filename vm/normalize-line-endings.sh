#!/usr/bin/env bash
# Strip CR from text files under a directory. Run on the VM after a migration.
#
# WHY THIS IS NEEDED EVERY TIME
#
# The repo is checked out on Windows, where Git's autocrlf writes CRLF into the
# working tree. Copying that tree to a Linux host copies the CRLF with it, and
# on Linux a CR is not whitespace -- it is part of the token:
#
#   #!/usr/bin/env bash^M      ->  bad interpreter: no such file or directory
#   DB_PASSWORD=hunter2^M      ->  the password is "hunter2\r" and auth fails
#   . ./.env                   ->  $'\r': command not found
#
# The .gitattributes added alongside this fixes it for future checkouts, but it
# only applies when Git next writes the file, so an existing Windows working
# tree stays CRLF until it is re-checked-out. This normalises what was copied.
#
# Skips .git, virtualenvs, and anything with a NUL byte in the first block --
# a naive `sed -i 's/\r$//'` across a tree will happily corrupt a PNG.

set -uo pipefail
ROOT="${1:-/opt/lantern}"

[ -d "$ROOT" ] || { echo "not a directory: $ROOT" >&2; exit 1; }

python3 - "$ROOT" <<'PY'
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__"}
# Windows consumes these and expects CRLF; leaving them alone is deliberate.
KEEP_CRLF = {".ps1", ".cmd", ".bat"}

changed, skipped_binary, kept = [], 0, 0

for path in root.rglob("*"):
    if not path.is_file() or path.is_symlink():
        continue
    if SKIP_DIRS & set(path.parts):
        continue
    if path.suffix.lower() in KEEP_CRLF:
        kept += 1
        continue
    try:
        data = path.read_bytes()
    except OSError:
        continue
    if b"\r\n" not in data:
        continue
    if b"\x00" in data[:8192]:
        skipped_binary += 1
        continue
    path.write_bytes(data.replace(b"\r\n", b"\n"))
    changed.append(path.relative_to(root))

for p in sorted(changed):
    print(f"  fixed  {p}")
print()
print(f"  {len(changed)} files normalised to LF")
if kept:
    print(f"  {kept} Windows scripts left as CRLF (.ps1/.cmd/.bat)")
if skipped_binary:
    print(f"  {skipped_binary} binary files skipped")
PY

# Prove it, rather than trusting the count above.
REMAIN=$(grep -rlU $'\r' "$ROOT" 2>/dev/null \
         | grep -vE '/\.git/|/\.venv/|node_modules|\.(ps1|cmd|bat|png|jpg|zip|vpk|so|dll|jar)$' \
         | wc -l)
if [ "$REMAIN" -gt 0 ]; then
  echo "  WARNING: $REMAIN files still contain CR:"
  grep -rlU $'\r' "$ROOT" 2>/dev/null \
    | grep -vE '/\.git/|/\.venv/|node_modules|\.(ps1|cmd|bat|png|jpg|zip|vpk|so|dll|jar)$' \
    | head -10 | sed 's/^/    /'
  exit 1
fi
echo "  verified: no CR left in text files"

# The scripts also need to be executable; tar preserves the Windows filesystem's
# idea of that, which is nothing useful.
find "$ROOT" -name '*.sh' -not -path '*/.git/*' -exec chmod +x {} +
[ -f "$ROOT/stack/lantern" ] && chmod +x "$ROOT/stack/lantern"
echo "  shell scripts made executable"
