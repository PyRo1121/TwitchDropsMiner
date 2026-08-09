#!/usr/bin/env bash
set -euo pipefail

script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P)
venv_dir="$script_dir/.venv"

if [ ! -d "$venv_dir" ]; then
	printf '\nCreating %s...\n' "$venv_dir"
	python3 -m venv "$venv_dir"
fi

printf '\nInstalling locked build dependencies...\n'
"$venv_dir/bin/python" -m pip install -r "$script_dir/requirements-build.txt"

printf '\nEnvironment setup completed successfully.\n\n'
