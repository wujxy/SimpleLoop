#!/usr/bin/env bash
set -euo pipefail

workspace="${1:?usage: eval.sh WORKSPACE --assets DIR [--evtmax N]}"
shift
assets=""
evtmax=100
while [[ $# -gt 0 ]]; do
    case "$1" in
        --assets) assets="$2"; shift 2 ;;
        --evtmax) evtmax="$2"; shift 2 ;;
        *) printf 'unknown evaluator option: %s\n' "$1" >&2; exit 2 ;;
    esac
done
test -d "$workspace"
test -d "$assets"
test -f "$assets/scripts/sl_eval_v100.sh"

project="$(mktemp -d /tmp/simpleloop-omilrec-eval.XXXXXX)"
trap 'rm -rf -- "$project"' EXIT
cp -a "$workspace/." "$project/"
rm -rf -- "$project/.git" "$project/scripts" "$project/tests" \
    "$project/baseline" "$project/benchmarks" "$project/reference"
for name in scripts tests baseline benchmarks; do
    if [[ -e "$assets/$name" ]]; then
        cp -a "$assets/$name" "$project/$name"
    fi
done
pytest_path=/usr/local/lib/cvmfs_python311_extra
if [[ ! -d "$pytest_path" ]]; then
    pytest_path=/usr/local/lib/python3.9/site-packages
fi
sed -i "/^ROOT_LIB=/i export PYTHONPATH=$pytest_path:\${PYTHONPATH:-}" \
    "$project/scripts/sl_eval_v100.sh"
bash "$project/scripts/sl_eval_v100.sh" --evtmax "$evtmax"
