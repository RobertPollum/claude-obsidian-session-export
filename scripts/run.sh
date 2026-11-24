#!/bin/sh
# Find a working Python 3.10+ (on Windows, python3 is often only the Microsoft Store stub).
dir=$(cd "$(dirname "$0")" && pwd)
command -v cygpath >/dev/null 2>&1 && dir=$(cygpath -m "$dir")
check='import sys; sys.exit(sys.version_info < (3, 10))'

for py in "$OBSIDIAN_EXPORT_PYTHON" python3 python py; do
  [ -n "$py" ] || continue
  if "$py" -c "$check" </dev/null >/dev/null 2>&1; then
    exec "$py" "$dir/export_claude_chats.py" "$@"
  fi
done

if [ "$1" = "--hook" ]; then
  echo '{"systemMessage":"obsidian-session-export: Python 3.10+ not found, so this session is not being exported."}'
  exit 0
fi
echo "Python 3.10+ not found. Install it or set OBSIDIAN_EXPORT_PYTHON." >&2
exit 1
