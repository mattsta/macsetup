#!/bin/sh
set -eu

script_dir=$(CDPATH= cd "$(dirname "$0")" && pwd)
repo_root=$(dirname "$script_dir")

exec uv --directory "$repo_root" run macsetup-transfer rsync-daemon-server "$@"
