#!/usr/bin/env bash
# Build a self-extracting single-file installer: dist/oracle-keepalive-installer.sh
#
#   ./make-bundle.sh                 build from the committed files (refuses if they are modified)
#   ./make-bundle.sh --out DIR       write into DIR instead of ./dist
#   ./make-bundle.sh --allow-dirty   build even if tracked files differ from HEAD (tests only)
#
# The bundle carries oracle-keepalive.py, the unit, the config example, install.sh, LICENSE and
# README.md as a gzip tarball (base64) behind a marker line, plus the SHA-256 of that tarball in
# its header. Running it verifies the checksum, unpacks to a temp dir, and hands every argument to
# the bundled install.sh. It never downloads anything. The checksum guards against a damaged or
# truncated copy; it is NOT a signature, so compare the bundle's own SHA-256 against the value you
# were given out of band before running it as root.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
FILES=(oracle-keepalive.py oracle-keepalive.service oracle-keepalive.conf.example install.sh LICENSE README.md)
OUT_DIR="dist"
ALLOW_DIRTY=0

while [ "$#" -gt 0 ]; do
    case "$1" in
        --out)         OUT_DIR="${2:?--out needs a directory}"; shift 2 ;;
        --allow-dirty) ALLOW_DIRTY=1; shift ;;
        -h|--help)     sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)             echo "error: unknown argument: $1" >&2; exit 2 ;;
    esac
done

for f in "${FILES[@]}"; do
    [ -f "$f" ] || { echo "error: missing file: $f" >&2; exit 1; }
done

COMMIT="unknown"
if command -v git >/dev/null 2>&1 && git rev-parse --git-dir >/dev/null 2>&1; then
    COMMIT="$(git rev-parse --short=12 HEAD 2>/dev/null || echo unknown)"
    if [ "$ALLOW_DIRTY" -eq 0 ]; then
        if ! git diff --quiet HEAD -- "${FILES[@]}" 2>/dev/null; then
            echo "error: bundled files differ from HEAD; commit them first (or use --allow-dirty)" >&2
            exit 1
        fi
    else
        COMMIT="${COMMIT}-dirty"
    fi
fi
VERSION="$(sed -n 's/^VERSION = "\(.*\)"/\1/p' oracle-keepalive.py | head -n 1)"
[ -n "$VERSION" ] || VERSION="unknown"

mkdir -p "$OUT_DIR"
OUT="${OUT_DIR%/}/oracle-keepalive-installer.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# Deterministic tarball: sorted names, fixed mtime, root ownership, no gzip timestamp.
tar --sort=name --mtime='@0' --owner=0 --group=0 --numeric-owner --mode='u+rwX,go+rX,go-w' \
    -cf - "${FILES[@]}" | gzip -n -9 > "$WORK/payload.tar.gz"
PAYLOAD_SHA="$(sha256sum "$WORK/payload.tar.gz" | awk '{print $1}')"
PAYLOAD_BYTES="$(stat -c %s "$WORK/payload.tar.gz")"

{
cat <<HEADER
#!/usr/bin/env bash
# oracle-keepalive self-extracting installer
#   version : ${VERSION}
#   commit  : ${COMMIT}
#   payload : ${PAYLOAD_BYTES} bytes (gzip tar), sha256 ${PAYLOAD_SHA}
#
# usage:  sudo bash $(basename "$OUT") [install.sh options]     e.g. --dry-run, --uninstall, --replace-eat
#         bash $(basename "$OUT") --show-info                    print version and checksums, change nothing
#         bash $(basename "$OUT") --extract DIR                  unpack the files into DIR, change nothing else
set -euo pipefail

BUNDLE_VERSION="${VERSION}"
BUNDLE_COMMIT="${COMMIT}"
PAYLOAD_SHA256="${PAYLOAD_SHA}"

die() { printf 'error: %s\n' "\$*" >&2; exit 1; }

SELF="\${BASH_SOURCE[0]}"
[ -r "\${SELF}" ] || die "cannot read this script (\${SELF}); run it from a file, not from a pipe"

START="\$(awk '/^__PAYLOAD_BELOW__\$/ { print NR + 1; exit }' "\${SELF}")"
[ -n "\${START}" ] || die "payload marker not found; the file is damaged"

unpack_to() {  # unpack_to DIR : verify the checksum, then extract
    local dir="\$1" tgz
    tgz="\${dir}/payload.tar.gz"
    tail -n +"\${START}" "\${SELF}" | base64 -d > "\${tgz}" 2>/dev/null || die "payload is not valid base64; the file is damaged or truncated"
    local got
    got="\$(sha256sum "\${tgz}" | awk '{print \$1}')"
    if [ "\${got}" != "\${PAYLOAD_SHA256}" ]; then
        die "checksum mismatch (expected \${PAYLOAD_SHA256}, got \${got}); the file is damaged or was modified. Nothing was installed."
    fi
    mkdir -p "\${dir}/src"
    tar -xzf "\${tgz}" -C "\${dir}/src" || die "cannot unpack the payload"
}

case "\${1:-}" in
    --show-info)
        printf 'oracle-keepalive installer\n  version : %s\n  commit  : %s\n  payload : sha256 %s\n  file    : sha256 %s\n' \\
            "\${BUNDLE_VERSION}" "\${BUNDLE_COMMIT}" "\${PAYLOAD_SHA256}" "\$(sha256sum "\${SELF}" | awk '{print \$1}')"
        exit 0 ;;
    --extract)
        DEST="\${2:?--extract needs a directory}"
        [ ! -e "\${DEST}" ] || [ -d "\${DEST}" ] || die "\${DEST} exists and is not a directory"
        mkdir -p "\${DEST}"
        TMP="\$(mktemp -d)"; trap 'rm -rf "\${TMP}"' EXIT
        unpack_to "\${TMP}"
        cp -a "\${TMP}/src/." "\${DEST}/"
        printf 'unpacked to %s (checksum verified)\n' "\${DEST}"
        exit 0 ;;
esac

TMP="\$(mktemp -d)"; trap 'rm -rf "\${TMP}"' EXIT
unpack_to "\${TMP}"
printf 'oracle-keepalive %s (%s): checksum verified, running install.sh %s\n' "\${BUNDLE_VERSION}" "\${BUNDLE_COMMIT}" "\$*"
bash "\${TMP}/src/install.sh" "\$@"
exit \$?

__PAYLOAD_BELOW__
HEADER
base64 -w 76 "$WORK/payload.tar.gz"
} > "$OUT"
chmod 0755 "$OUT"

echo "built $OUT"
echo "  version : $VERSION"
echo "  commit  : $COMMIT"
echo "  payload : $PAYLOAD_BYTES bytes, sha256 $PAYLOAD_SHA"
echo "  file    : $(stat -c %s "$OUT") bytes, sha256 $(sha256sum "$OUT" | awk '{print $1}')"
