#!/usr/bin/env bash
set -euo pipefail

script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P)
venv_dir="$script_dir/.venv"

if ! python3 -c 'import sys; raise SystemExit(sys.version_info[:2] != (3, 10))'; then
	printf '\nRelease builds require CPython 3.10.\n\n' >&2
	exit 1
fi

if [ ! -d "$venv_dir" ]; then
	printf '\nCreating %s...\n' "$venv_dir"
	python3 -m venv "$venv_dir"
fi

if ! "$venv_dir/bin/python" -c 'import sys; raise SystemExit(sys.version_info[:2] != (3, 10))'; then
	printf '\nExisting .venv does not use CPython 3.10; replace it deliberately.\n\n' >&2
	exit 1
fi

printf '\nInstalling hash-locked packaging bootstrap...\n'
"$venv_dir/bin/python" -m pip install \
	--require-hashes --only-binary=:all: \
	-r "$script_dir/requirements-bootstrap.txt"

printf '\nInstalling hash-locked build dependencies...\n'
"$venv_dir/bin/python" -m pip install \
	--require-hashes --only-binary=:all: \
	-r "$script_dir/requirements-build.txt"

printf '\nEnvironment setup completed successfully.\n\n'
