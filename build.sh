#!/usr/bin/env bash
set -euo pipefail

script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P)
venv_dir="$script_dir/.venv"

if [ ! -x "$venv_dir/bin/pyinstaller" ]; then
	printf '\nNo build environment found. Run setup_env.sh first.\n\n' >&2
	exit 1
fi

printf '\nBuilding...\n'
cd "$script_dir"
"$venv_dir/bin/pyinstaller" --clean --noconfirm "$script_dir/build.spec"
printf '\nBuild completed successfully.\n\n'
