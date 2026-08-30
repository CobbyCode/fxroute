#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-}"
[[ "$SOURCE_DATE_EPOCH" =~ ^[0-9]+$ ]] || {
  printf '%s\n' 'mkisofs wrapper requires a non-negative SOURCE_DATE_EPOCH' >&2
  exit 2
}

output=""
args=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    -o)
      [[ $# -ge 2 ]] || {
        printf '%s\n' 'mkisofs wrapper: -o requires a path' >&2
        exit 2
      }
      output="$2"
      shift 2
      ;;
    *)
      args+=("$1")
      shift
      ;;
  esac
done

[[ -n "$output" ]] || {
  printf '%s\n' 'mkisofs wrapper: no output path was supplied' >&2
  exit 2
}

reproducible_date="$(date -u --date="@$SOURCE_DATE_EPOCH" '+%Y-%m-%d %H:%M:%S +0000')"

# mkisofs still reads timestamps from directory records, so normalize every
# source tree named by mkmedia before building the image.
for ((index = 0; index < ${#args[@]}; index++)); do
  if [[ "${args[index]}" == -path-list ]]; then
    index=$((index + 1))
    path_list="${args[index]:-}"
    [[ -f "$path_list" ]] || {
      printf 'mkisofs wrapper: path list not found: %s\n' "$path_list" >&2
      exit 2
    }
    while IFS= read -r source; do
      [[ -n "$source" ]] || continue
      if [[ ! -e "$source" && ! -L "$source" && "$source" == *=* ]]; then
        source="${source#*=}"
      fi
      [[ -e "$source" || -L "$source" ]] || {
        printf 'mkisofs wrapper: source path not found: %s\n' "$source" >&2
        exit 2
      }
      find "$source" -exec touch -h --date="@$SOURCE_DATE_EPOCH" {} +
    done < "$path_list"
  fi
done

log_file="$(mktemp "${TMPDIR:-/tmp}/fxroute-mkisofs.XXXXXX")"
cleanup() {
  rm -f -- "$log_file"
}
trap cleanup EXIT

# mkisofs only enables its complete reproducibility mode when it writes the
# image to stdout. Keep its diagnostics on the mkmedia pipe instead.
if TZ=Europe/Berlin LC_ALL=C "${FXROUTE_REAL_MKISOFS:-/usr/bin/mkisofs}" \
  -noatime \
  -reproducible-date "$reproducible_date" \
  "${args[@]}" \
  >"$output" 2>"$log_file"; then
  status=0
else
  status=$?
fi
if [[ "$status" -eq 0 ]]; then
  python3 - "$output" <<'PY' || status=$?
import sys
from collections import deque


SECTOR_SIZE = 2048
DATE_FLAGS = (1, 2, 4, 8, 16, 32, 64)


def get_733(data, offset):
    return int.from_bytes(data[offset : offset + 4], "little")


def normalize_tf(data, offset, length):
    if length < 5:
        return False

    flags = data[offset + 4]
    date_size = 17 if flags & 0x80 else 7
    cursor = offset + 5
    dates = []
    for flag in DATE_FLAGS:
        if not flags & flag:
            continue
        if cursor + date_size > offset + length:
            return False
        dates.append((flag, cursor))
        cursor += date_size

    modification = next((start for flag, start in dates if flag == 2), None)
    attribute = next((start for flag, start in dates if flag == 8), None)
    if modification is None or attribute is None:
        return False

    replacement = bytes(data[modification : modification + date_size])
    current = bytes(data[attribute : attribute + date_size])
    if current == replacement:
        return False
    data[attribute : attribute + date_size] = replacement
    return True


with open(sys.argv[1], "r+b") as image:
    image.seek(16 * SECTOR_SIZE)
    primary_volume_descriptor = image.read(SECTOR_SIZE)
    if len(primary_volume_descriptor) != SECTOR_SIZE:
        raise RuntimeError("mkisofs output has no complete primary volume descriptor")
    if primary_volume_descriptor[1:6] != b"CD001":
        raise RuntimeError("mkisofs output is not an ISO-9660 image")

    file_size = image.seek(0, 2)
    root = primary_volume_descriptor[156:190]
    directories = deque([(get_733(root, 2), get_733(root, 10))])
    seen_directories = set()
    seen_continuations = set()

    def read_at(offset, length):
        image.seek(offset)
        value = image.read(length)
        if len(value) != length:
            raise RuntimeError("mkisofs output ended while reading ISO metadata")
        return value

    def normalize_system_use(data, absolute_offset):
        changed = False
        cursor = 0
        while cursor + 4 <= len(data):
            signature = bytes(data[cursor : cursor + 2])
            entry_length = data[cursor + 2]
            if entry_length < 4 or cursor + entry_length > len(data):
                break
            if signature == b"TF":
                changed |= normalize_tf(data, cursor, entry_length)
            elif signature == b"CE" and entry_length >= 28:
                continuation_extent = get_733(data, cursor + 4)
                continuation_offset = get_733(data, cursor + 12)
                continuation_length = get_733(data, cursor + 20)
                continuation_key = (
                    continuation_extent,
                    continuation_offset,
                    continuation_length,
                )
                if continuation_length and continuation_key not in seen_continuations:
                    seen_continuations.add(continuation_key)
                    continuation_absolute_offset = (
                        continuation_extent * SECTOR_SIZE + continuation_offset
                    )
                    continuation = bytearray(
                        read_at(continuation_absolute_offset, continuation_length)
                    )
                    if normalize_system_use(
                        continuation, continuation_absolute_offset
                    ):
                        image.seek(continuation_absolute_offset)
                        image.write(continuation)
            cursor += entry_length
        return changed

    while directories:
        extent, directory_size = directories.popleft()
        directory_key = (extent, directory_size)
        if directory_key in seen_directories:
            continue
        seen_directories.add(directory_key)
        directory_absolute_offset = extent * SECTOR_SIZE
        if directory_absolute_offset + directory_size > file_size:
            raise RuntimeError("ISO directory extends beyond the mkisofs output")

        position = 0
        while position < directory_size:
            sector_offset = position % SECTOR_SIZE
            sector_absolute_offset = directory_absolute_offset + position - sector_offset
            sector = read_at(sector_absolute_offset, SECTOR_SIZE)
            record_length = sector[sector_offset]
            if record_length == 0:
                position += SECTOR_SIZE - sector_offset
                continue
            if (
                record_length < 34
                or sector_offset + record_length > SECTOR_SIZE
                or position + record_length > directory_size
            ):
                raise RuntimeError("invalid ISO directory record in mkisofs output")

            record_absolute_offset = directory_absolute_offset + position
            record = bytearray(sector[sector_offset : sector_offset + record_length])
            name_length = record[32]
            name_end = 33 + name_length
            if name_end <= record_length:
                name = bytes(record[33:name_end])
                if record[25] & 2 and name not in (b"\x00", b"\x01"):
                    directories.append((get_733(record, 2), get_733(record, 10)))
                system_use_offset = name_end + (name_length % 2 == 0)
                if system_use_offset < record_length:
                    system_use = bytearray(record[system_use_offset:])
                    if normalize_system_use(
                        system_use, record_absolute_offset + system_use_offset
                    ):
                        record[system_use_offset:] = system_use
                        image.seek(record_absolute_offset)
                        image.write(record)
            position += record_length
PY
fi
cat -- "$log_file"
exit "$status"
