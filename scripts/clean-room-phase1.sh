#!/bin/bash
set -euo pipefail

usage() {
    printf '%s\n' \
        'usage: clean-room-phase1.sh --python PATH --wheelhouse DIR --result FILE --commit SHA' \
        'required: --python --wheelhouse --result --commit' >&2
    exit 2
}

python_path=''
wheelhouse=''
result_path=''
source_commit=''
while (($#)); do
    case "$1" in
        --python|--wheelhouse|--result|--commit)
            (($# >= 2)) || usage
            option=$1
            value=$2
            shift 2
            case "$option" in
                --python) python_path=$value ;;
                --wheelhouse) wheelhouse=$value ;;
                --result) result_path=$value ;;
                --commit) source_commit=$value ;;
            esac
            ;;
        *) usage ;;
    esac
done

[[ -n "$python_path" && -n "$wheelhouse" && -n "$result_path" && -n "$source_commit" ]] || usage
[[ "$python_path" = /* && "$wheelhouse" = /* && "$result_path" = /* ]] || usage
[[ -x "$python_path" && ! -L "$python_path" ]] || { printf '%s\n' 'invalid Python interpreter' >&2; exit 2; }
[[ -d "$wheelhouse" && ! -L "$wheelhouse" ]] || { printf '%s\n' 'invalid wheelhouse' >&2; exit 2; }
[[ "$source_commit" =~ ^[0-9a-f]{40}$ ]] || { printf '%s\n' 'commit must be a full SHA' >&2; exit 2; }

git_command=(env GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null git)
repository_root=$("${git_command[@]}" rev-parse --show-toplevel)
[[ "$PWD" = "$repository_root" ]] || { printf '%s\n' 'run from the repository root' >&2; exit 2; }
[[ -z "$("${git_command[@]}" status --porcelain=v1 --untracked-files=all)" ]] || {
    printf '%s\n' 'source checkout is not clean' >&2
    exit 2
}
resolved_commit=$("${git_command[@]}" rev-parse --verify "${source_commit}^{commit}")
[[ "$resolved_commit" = "$source_commit" ]] || { printf '%s\n' 'commit did not resolve exactly' >&2; exit 2; }
[[ "$("${git_command[@]}" rev-parse HEAD)" = "$source_commit" ]] || { printf '%s\n' 'commit is not checked out at HEAD' >&2; exit 2; }

result_parent=$(dirname "$result_path")
[[ -d "$result_parent" && ! -L "$result_parent" ]] || { printf '%s\n' 'invalid result parent' >&2; exit 2; }
"$python_path" -I -c \
    'import os,pathlib,stat,sys; checkout=pathlib.Path(sys.argv[1]).resolve(); target=pathlib.Path(sys.argv[2]); parent=target.parent.resolve(strict=True); assert parent != checkout and not parent.is_relative_to(checkout), "result must be outside checkout"; assert not target.is_symlink(), "result target must not be a symlink"; exists=target.exists(); assert not exists or (stat.S_ISREG(target.stat().st_mode) and target.stat().st_uid == os.geteuid()), "result target must be absent or an owned regular file"' \
    "$repository_root" "$result_path" || { printf '%s\n' 'invalid result target' >&2; exit 2; }

umask 077
isolated_root=$(mktemp -d "${TMPDIR:-/tmp}/music-friend-clean-room.XXXXXXXX")
case "$isolated_root" in
    "${TMPDIR:-/tmp}"/music-friend-clean-room.*) ;;
    *) printf '%s\n' 'unexpected temporary root' >&2; exit 2 ;;
esac

stage='initialize'
completed=0
coverage_summary() {
    "$python_path" -I -c \
        'import json,sys; summary=json.load(open(sys.argv[1],encoding="utf-8"))["totals"]; total=summary["num_statements"]; missed=summary["missing_lines"]; print(json.dumps({"percentage":round(summary["percent_covered"],2),"statements":total,"covered":total-missed,"missed":missed,"threshold":95},sort_keys=True,separators=(",",":")))' \
        "$1"
}
write_failure() {
    failure_temporary=$(mktemp "$result_parent/.music-friend-clean-room-failure.XXXXXXXX")
    failure_coverage='null'
    if [[ -f "${build_root:-}/coverage.json" ]]; then
        failure_coverage=$(coverage_summary "$build_root/coverage.json")
    fi
    "$python_path" -I -c \
        'import json,sys; payload={"certification":"development-clean-room","status":"fail","commit":sys.argv[1],"failed_stage":sys.argv[2]}; coverage=json.loads(sys.argv[3]); payload.update({"coverage":coverage} if coverage is not None else {}); json.dump(payload,open(sys.argv[4],"w",encoding="utf-8"),sort_keys=True,separators=(",",":")); open(sys.argv[4],"a",encoding="utf-8").write("\n")' \
        "$source_commit" "$stage" "$failure_coverage" "$failure_temporary"
    mv -f "$failure_temporary" "$result_path"
}
finish() {
    exit_status=$?
    if ((completed == 1 && exit_status == 0)); then
        case "$isolated_root" in
            "${TMPDIR:-/tmp}"/music-friend-clean-room.*) rm -rf -- "$isolated_root" ;;
            *) printf '%s\n' 'refused temporary cleanup' >&2; exit 2 ;;
        esac
    else
        write_failure || true
    fi
}
trap finish EXIT

archive_path="$isolated_root/source.tar"
source_root="$isolated_root/source"
home_root="$isolated_root/home"
config_root="$isolated_root/config"
cache_root="$isolated_root/cache"
temporary_root="$isolated_root/tmp"
venv_root="$isolated_root/venv"
build_root="$isolated_root/build"
pytest_root="$isolated_root/pytest"
logs_root="$isolated_root/logs"
boundary_result="$isolated_root/boundaries.json"
mkdir -p "$source_root" "$home_root" "$config_root" "$cache_root" "$temporary_root" \
    "$build_root" "$pytest_root" "$logs_root"

isolated_env=(
    env -i
    "HOME=$home_root"
    "XDG_CONFIG_HOME=$config_root"
    "XDG_CACHE_HOME=$cache_root"
    "TMPDIR=$temporary_root"
    "PIP_CONFIG_FILE=/dev/null"
    "PIP_NO_INDEX=1"
    "PIP_FIND_LINKS=$wheelhouse"
    "PIP_CACHE_DIR=$cache_root/pip"
    "PYTHONNOUSERSITE=1"
    "PYTHONDONTWRITEBYTECODE=1"
    "COVERAGE_FILE=$build_root/.coverage"
    "PATH=/usr/bin:/bin"
    "LC_ALL=C"
    "MF_PHASE1_NESTED=${MF_PHASE1_NESTED:-}"
)
if [[ $(uname -s) = Darwin ]]; then
    developer_dir=$(xcode-select -p)
    isolated_env+=("DEVELOPER_DIR=$developer_dir")
fi

stage='validate-wheelhouse'
wheelhouse_digest=$("$python_path" -I -c \
    'import hashlib,pathlib,sys; root=pathlib.Path(sys.argv[1]); entries=sorted(root.iterdir(),key=lambda p:p.name); assert entries and all(p.is_file() and not p.is_symlink() and p.suffix==".whl" for p in entries), "wheelhouse must contain only regular wheel files"; h=hashlib.sha256(); [(h.update(p.name.encode()+b"\0"),h.update(hashlib.sha256(p.read_bytes()).digest())) for p in entries]; print(h.hexdigest())' \
    "$wheelhouse")
interpreter_version=$("$python_path" -I -c 'import platform; print(platform.python_version())')
interpreter_digest=$("$python_path" -I -c \
    'import hashlib,pathlib,sys; print(hashlib.sha256(pathlib.Path(sys.executable).read_bytes()).hexdigest())')

stage='archive-source'
"${git_command[@]}" archive --format=tar --output="$archive_path" "$source_commit"
archive_digest=$(shasum -a 256 "$archive_path" | awk '{print $1}')
"${isolated_env[@]}" "$python_path" -I "$repository_root/scripts/scan_public_tree.py" \
    "$archive_path" >"$logs_root/archive-scan.json" 2>"$logs_root/archive-scan.err"
tar -xf "$archive_path" -C "$source_root"

stage='create-environment'
"${isolated_env[@]}" "$python_path" -I -m venv --copies "$venv_root" \
    >"$logs_root/venv.log" 2>&1
venv_python="$venv_root/bin/python"

stage='install-export'
"${isolated_env[@]}" "$venv_python" -I -m pip install \
    --no-index --find-links "$wheelhouse" hatchling "${source_root}[dev]" \
    >"$logs_root/install.log" 2>&1

stage='scan-export'
"${isolated_env[@]}" "$venv_python" -I "$source_root/scripts/scan_public_tree.py" "$source_root" \
    >"$logs_root/source-scan.json" 2>"$logs_root/source-scan.err"

stage='prepare-test-checkout'
(cd "$source_root" && \
    "${git_command[@]}" init -q && \
    "${git_command[@]}" add -A && \
    env GIT_AUTHOR_NAME='Clean Room' GIT_AUTHOR_EMAIL='clean-room@example.invalid' \
        GIT_COMMITTER_NAME='Clean Room' GIT_COMMITTER_EMAIL='clean-room@example.invalid' \
        GIT_AUTHOR_DATE='2000-01-01T00:00:00+00:00' \
        GIT_COMMITTER_DATE='2000-01-01T00:00:00+00:00' \
        "${git_command[@]}" commit -q -m 'certification source')

stage='import-package'
(cd "$temporary_root" && "${isolated_env[@]}" "$venv_python" -I -c \
    'import music_friend,pathlib,site; package=pathlib.Path(music_friend.__file__).resolve(); roots=[pathlib.Path(value).resolve() for value in site.getsitepackages()]; assert any(package.is_relative_to(root) for root in roots), "package import did not resolve beneath isolated site-packages"')

write_roots=$("$venv_python" -I -c \
    'import json,sys; names=("venv","build","pytest","temporary","boundary-evidence"); print(json.dumps(dict(zip(names,sys.argv[1:]))))' \
    "$venv_root" "$build_root" "$pytest_root" "$temporary_root" "$boundary_result")
allowed_children='["git-ls-source","git-ls-all","git-source-head","git-archive-head","git-test-init","git-test-add","git-test-commit","git-test-head","python-build-export","python-build-source","python-artifact-smoke","python-wheel-venv","python-wheel-install","installed-wheel-skill","catalog-mcp-stdio","hatchling-build","scanner-source","scanner-test","harness-certification","harness-empty","spotify-harness"]'

test_env=(
    "${isolated_env[@]}"
    "PYTHONPATH=$source_root/tests"
    "MF_CLEAN_ROOM_ACTIVE=1"
    "MF_CLEAN_ROOM_WRITE_ROOTS=$write_roots"
    "MF_CLEAN_ROOM_CHILD_COMMANDS=$allowed_children"
    "MF_CLEAN_ROOM_BOUNDARY_RESULT=$boundary_result"
    "MF_PHASE1_TEST_PYTHON=$python_path"
    "MF_PHASE1_TEST_WHEELHOUSE=$wheelhouse"
)

stage='run-tests'
(cd "$source_root" && "${test_env[@]}" "$venv_python" -m pytest -q -p no:cacheprovider \
    -p clean_room.boundaries --basetemp "$pytest_root" --log-file "$pytest_root/phase1.log" \
    --cov=music_friend \
    --cov-report=term-missing --cov-report="json:$build_root/coverage.json" --cov-fail-under=95 \
    >"$logs_root/pytest.log" 2>&1)

stage='typecheck'
(cd "$source_root" && "${isolated_env[@]}" "$venv_python" -I -m mypy src/music_friend \
    --cache-dir "$cache_root/mypy" >"$logs_root/mypy.log" 2>&1)

stage='lint'
(cd "$source_root" && "${isolated_env[@]}" "$venv_python" -I -m ruff check \
    src tests scripts --select E4,E7,E9,F --no-cache >"$logs_root/ruff.log" 2>&1)
(cd "$source_root" && "${isolated_env[@]}" "$venv_python" -I -m ruff format --check \
    src tests scripts >"$logs_root/ruff-format.log" 2>&1)

stage='build-artifacts'
(cd "$source_root" && "${isolated_env[@]}" "$venv_python" -I -m build \
    --no-isolation --outdir "$build_root" >"$logs_root/build.log" 2>&1)

wheel_artifacts=()
source_artifacts=()
while IFS= read -r artifact; do
    wheel_artifacts+=("$artifact")
done < <(find "$build_root" -maxdepth 1 -type f -name '*.whl' -print | sort)
while IFS= read -r artifact; do
    source_artifacts+=("$artifact")
done < <(find "$build_root" -maxdepth 1 -type f -name '*.tar.gz' -print | sort)
[[ ${#wheel_artifacts[@]} -eq 1 && ${#source_artifacts[@]} -eq 1 ]] || {
    printf '%s\n' 'build did not produce exactly one wheel and one source distribution' >&2
    false
}
artifacts=("${wheel_artifacts[0]}" "${source_artifacts[0]}")

stage='scan-artifacts'
artifact_arguments=()
for artifact in "${artifacts[@]}"; do
    "${isolated_env[@]}" "$venv_python" -I "$source_root/scripts/scan_public_tree.py" "$artifact" \
        >"$logs_root/$(basename "$artifact").scan.json" \
        2>"$logs_root/$(basename "$artifact").scan.err"
    artifact_arguments+=("$(basename "$artifact")")
    artifact_arguments+=("$(shasum -a 256 "$artifact" | awk '{print $1}')")
done

stage='write-evidence'
boundary_payload=$("$venv_python" -I -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).read_text())' "$boundary_result")
coverage_payload=$(coverage_summary "$build_root/coverage.json")
result_temporary=$(mktemp "$result_parent/.music-friend-clean-room-result.XXXXXXXX")
"$venv_python" -I -c \
    'import json,sys; pairs=sys.argv[9:]; artifacts=[{"name":pairs[i],"sha256":pairs[i+1]} for i in range(0,len(pairs),2)]; payload={"archive_sha256":sys.argv[2],"artifacts":artifacts,"boundaries":json.loads(sys.argv[7]),"certification":"development-clean-room","commands":["archive-source","create-environment","offline-install","scan-export","prepare-test-checkout","import-package","pytest","typecheck","lint","build-artifacts","scan-artifacts"],"commit":sys.argv[1],"coverage":json.loads(sys.argv[8]),"interpreter":{"sha256":sys.argv[4],"version":sys.argv[3]},"status":"pass","wheelhouse_sha256":sys.argv[5]}; json.dump(payload,open(sys.argv[6],"w",encoding="utf-8"),sort_keys=True,separators=(",",":")); open(sys.argv[6],"a",encoding="utf-8").write("\n")' \
    "$source_commit" "$archive_digest" "$interpreter_version" "$interpreter_digest" \
    "$wheelhouse_digest" "$result_temporary" "$boundary_payload" "$coverage_payload" \
    "${artifact_arguments[@]}"
mv -f "$result_temporary" "$result_path"
chmod 600 "$result_path"
[[ $(wc -c <"$result_path") -le 16384 ]] || { printf '%s\n' 'evidence exceeded size limit' >&2; false; }
completed=1
