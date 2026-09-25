#!/usr/bin/env bash
# Reproducible-build check (SPEC §8.10): rebuild-and-diff.
#
# Builds the sdist and the wheel twice from this checkout with the same SOURCE_DATE_EPOCH (default:
# the committer time of HEAD) into two fresh directories and fails if any artifact's SHA-256
# differs. With a reference directory (e.g. the release job's dist/, or files downloaded from
# PyPI / the GitHub release), the rebuilt artifacts must also match it byte for byte; the
# rebuild then pins hatchling to the version recorded in the reference wheel's WHEEL file
# ("Generator: hatchling X.Y.Z"), so a later verifier reproduces the same backend.
#
# Usage:
#   scripts/repro_check.sh                 # build twice, compare
#   scripts/repro_check.sh dist            # ... and compare with dist/
#   SOURCE_DATE_EPOCH=1790367302 scripts/repro_check.sh dist
#
# Needs: uv, git, python3 (diagnostics only), sha256sum or shasum. Exit 0 = reproducible.
set -euo pipefail

reference="${1:-}"
root="$(git rev-parse --show-toplevel)"
cd "$root"

if [ -z "${SOURCE_DATE_EPOCH:-}" ]; then
  SOURCE_DATE_EPOCH="$(git log -1 --format=%ct)"
fi
export SOURCE_DATE_EPOCH
# Normalize what the environment can leak into an archive: file modes and locale.
umask 022
export LC_ALL=C TZ=UTC PYTHONHASHSEED=0

if command -v sha256sum >/dev/null 2>&1; then
  sha256() { sha256sum "$1" | cut -d' ' -f1; }
else
  sha256() { shasum -a 256 "$1" | cut -d' ' -f1; }
fi

work="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/tokenbill-repro.XXXXXX")"
trap 'rm -rf "$work"' EXIT

constraints=()
if [ -n "$reference" ]; then
  [ -d "$reference" ] || { echo "repro_check: $reference is not a directory" >&2; exit 2; }
  ref_wheel="$(find "$reference" -maxdepth 1 -name 'tokenbill-*.whl' | head -n 1)"
  if [ -n "$ref_wheel" ]; then
    generator="$(python3 - "$ref_wheel" <<'PY'
import sys, zipfile
with zipfile.ZipFile(sys.argv[1]) as zf:
    name = next(n for n in zf.namelist() if n.endswith(".dist-info/WHEEL"))
    for line in zf.read(name).decode().splitlines():
        if line.startswith("Generator: hatchling "):
            print(line.split()[-1])
PY
)"
    if [ -n "$generator" ]; then
      echo "hatchling==$generator" > "$work/build-constraints.txt"
      constraints=(--build-constraints "$work/build-constraints.txt")
      echo "repro_check: pinning hatchling==$generator (from $(basename "$ref_wheel"))"
    fi
  fi
fi

echo "repro_check: SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH ($(git rev-parse --short HEAD))"
for run in a b; do
  # The release's own command (sdist, then the wheel from the sdist); --no-cache gives a fresh
  # build environment each time, so a cached one cannot mask a difference.
  uv build --no-cache --out-dir "$work/$run" ${constraints[@]+"${constraints[@]}"} \
    >"$work/build-$run.log" 2>&1 || { cat "$work/build-$run.log" >&2; exit 1; }
done

list() { (cd "$1" && find . -maxdepth 1 -type f \( -name '*.whl' -o -name '*.tar.gz' \) | sort); }
if [ "$(list "$work/a")" != "$(list "$work/b")" ]; then
  echo "repro_check: the two builds produced different file sets" >&2
  diff <(list "$work/a") <(list "$work/b") >&2 || true
  exit 1
fi
count="$(list "$work/a" | wc -l | tr -d ' ')"
if [ "$count" -ne 2 ]; then
  echo "repro_check: expected one sdist and one wheel, got $count file(s)" >&2
  list "$work/a" >&2
  exit 1
fi

zip_diff() {  # member-level diagnostics for two wheels (names, CRCs, timestamps, modes)
  python3 - "$1" "$2" <<'PY' || true
import sys, zipfile
def rows(p):
    with zipfile.ZipFile(p) as zf:
        return {i.filename: (i.CRC, i.date_time, i.external_attr >> 16) for i in zf.infolist()}
a, b = rows(sys.argv[1]), rows(sys.argv[2])
diff = [n for n in sorted(a.keys() | b.keys()) if a.get(n) != b.get(n)]
print(f"  {len(diff)} member(s) differ as (crc32, zip time, mode):")
for name in diff[:20]:
    print(f"  {name}: {a.get(name)} != {b.get(name)}")
if len(diff) > 20:
    print(f"  ... and {len(diff) - 20} more")
PY
}

status=0
compare() {  # compare <label> <dir-a> <dir-b>
  local label="$1" left="$2" right="$3" f ha hb
  while IFS= read -r f; do
    f="${f#./}"
    if [ ! -f "$right/$f" ]; then
      echo "MISSING  $f ($label)"; status=1; continue
    fi
    ha="$(sha256 "$left/$f")"; hb="$(sha256 "$right/$f")"
    if [ "$ha" = "$hb" ]; then
      echo "OK       $ha  $f ($label)"
    else
      echo "MISMATCH $f ($label)"; echo "  $ha"; echo "  $hb"; status=1
      case "$f" in *.whl) zip_diff "$left/$f" "$right/$f" ;; esac
    fi
  done < <(list "$left")
}

compare "rebuild a vs b" "$work/a" "$work/b"
if [ -n "$reference" ]; then
  compare "rebuild vs $reference" "$work/a" "$reference"
fi

if [ "$status" -ne 0 ]; then
  echo "repro_check: NOT reproducible" >&2
  exit 1
fi
echo "repro_check: reproducible"
