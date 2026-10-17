#!/bin/sh
# Usage: wait-for-it.sh host port [timeout_seconds] [-- command args...]
set -eu
host="$1"; port="$2"; timeout="${3:-60}"
shift 2; [ $# -gt 0 ] && shift
[ "${1:-}" = "--" ] && shift
i=0
# shellcheck disable=SC3025  # /dev/tcp works in bash/busybox ash; nc is the fallback
until (exec 3<>"/dev/tcp/$host/$port") 2>/dev/null || nc -z "$host" "$port" 2>/dev/null; do
  i=$((i + 1))
  [ "$i" -ge "$timeout" ] && { echo "timeout waiting for $host:$port" >&2; exit 1; }
  sleep 1
done
echo "$host:$port is up"
[ $# -gt 0 ] && exec "$@"
exit 0
