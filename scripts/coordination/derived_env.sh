#!/bin/sh
# Test environment for the derived packets of one repository.
#
#     scripts/coordination/derived_env.sh REPO [CHECKOUT]
#
# REPO is one of: click, werkzeug, black, pytest, pydantic, astroid, poetry, sphinx. CHECKOUT is needed only
# for pydantic and poetry: each pins its core exactly (pydantic-core==, poetry-core (==...)), so the
# checkout's pin names the environment (pydantic-core-2.48.0, poetry-core-2.5.0) and packets with the
# same pin share it. Every other repository has one environment. Print the interpreter to give `derived.py validate --python`
# as the last line of output.
#
# Environments live under $DERIVED_ENVS (default ~/.local/share/derived-staging/envs) and are built
# with $DERIVED_PYTHON (default python3.12). The script is safe to re-run: an environment that exists
# is kept and only gets the packages it lacks. Nothing is upgraded, removed or rebuilt, because
# finished validations and attempts point at these directories.
#
# bin/pysrc runs the interpreter with the checkout's root and src/ on PYTHONPATH, taken from the
# directory it is started in. The target package itself is NOT installed, only put on the path, so a
# packet whose tests need package metadata, entry points, a build step or a compiled extension of the
# repository cannot be judged here. Of the five repositories that rules out none whose packets have
# validated so far; pydantic gets its compiled core (pydantic-core) from PyPI, not from a build.
# Dependencies are the latest releases, except pydantic-core; the black, click, werkzeug and pytest
# packets that failed validation did so for other reasons (see docs/results/2026-09-30-first-stops.md
# in the router repository), so no date bound is applied. Add one here if a packet shows it is needed.
#
# KNOWN LIMIT: the hidden tests run the target repository's code as the owner's user, in this
# machine's environment, with no container and no network restriction. Keep to the eight repositories
# the owner has chosen (astroid, poetry and sphinx added 2026-09-30); do not use this for code that has not been read.
set -eu

repo=${1:-}
checkout=${2:-}
envs=${DERIVED_ENVS:-$HOME/.local/share/derived-staging/envs}
python=${DERIVED_PYTHON:-python3.12}

case $repo in
    click)
        name=click; specs="pytest" ;;
    werkzeug)
        name=werkzeug; specs="pytest markupsafe ephemeral-port-reserve watchdog cryptography pytest-timeout" ;;
    black)
        # ipython and tokenize-rt are Black's "jupyter" extra: tests/test_ipynb.py skips without them.
        name=black
        specs="pytest click mypy_extensions packaging pathspec platformdirs aiohttp tomli hypothesis pytokens ipython tokenize-rt" ;;
    pytest)
        # pytest's own dependencies, but not pytest: a released pytest in site-packages holds the merged code
        # of most packets, and the worker can read it. The checkout's src/ is on the path through bin/pysrc.
        name=pytest
        specs="exceptiongroup tomli pluggy iniconfig packaging pygments hypothesis xmlschema attrs mock argcomplete pexpect setuptools" ;;
    pydantic)
        if [ -z "$checkout" ] || [ ! -f "$checkout/pyproject.toml" ]; then
            echo "pydantic needs CHECKOUT, a directory with a pyproject.toml, to read the pydantic-core pin" >&2
            exit 2
        fi
        pin=$(grep -o 'pydantic-core==[0-9][0-9A-Za-z.]*' "$checkout/pyproject.toml" | head -n 1 || true)
        if [ -z "$pin" ]; then
            echo "No pydantic-core pin in $checkout/pyproject.toml" >&2
            exit 2
        fi
        name=pydantic-core-${pin#pydantic-core==}
        specs="$pin pytest pytest-mock pytest-examples dirty-equals faker jsonschema pytz typing-extensions typing-inspection annotated-types pytest-benchmark pytest-run-parallel" ;;
    astroid)
        # requirements_full.txt without PyQt6 and numpy (numpy is only required below Python 3.12); their brain tests skip.
        name=astroid
        specs="pytest attrs python-dateutil regex setuptools<82 six urllib3>1,<2 typing-extensions" ;;
    sphinx)
        # Runtime dependencies and the test group, without cython (only its Cython-compilation tests need it).
        # docutils keeps sphinx's own upper bound; roman-numerals-py is the older name of roman-numerals.
        name=sphinx
        specs="pytest pytest-xdist[psutil] defusedxml setuptools typing-extensions sphinxcontrib-applehelp sphinxcontrib-devhelp sphinxcontrib-htmlhelp sphinxcontrib-jsmath sphinxcontrib-qthelp sphinxcontrib-serializinghtml Jinja2 Pygments docutils<0.23 snowballstemmer babel alabaster imagesize requests roman-numerals roman-numerals-py packaging" ;;
    poetry)
        # KNOWN LIMIT: poetry's tests/conftest.py reads poetry's own package metadata, which a path-only
        # environment lacks, so none of the 30 poetry packets tried on 2026-09-30 validated here.
        if [ -z "$checkout" ] || [ ! -f "$checkout/pyproject.toml" ]; then
            echo "poetry needs CHECKOUT, a directory with a pyproject.toml, to read the poetry-core pin" >&2
            exit 2
        fi
        version=$(grep -o 'poetry-core (==[0-9][0-9A-Za-z.]*)' "$checkout/pyproject.toml" | head -n 1 | sed 's/.*==\(.*\))/\1/' || true)
        if [ -z "$version" ]; then
            echo "No poetry-core pin in $checkout/pyproject.toml" >&2
            exit 2
        fi
        name=poetry-core-$version
        # The runtime dependencies at the time of writing, then the test group. pytest-randomly is left out so
        # validation runs the tests in a fixed order; pytest-xdist is needed by the configured "-n logical".
        specs="poetry-core==$version build cachecontrol[filecache] cleo dulwich>=1.2.1 fastjsonschema installer keyring packaging pkginfo platformdirs pyproject-hooks requests requests-toolbelt shellingham tomlkit trove-classifiers virtualenv findpython pbs-installer[download,install] pytest pytest-mock pytest-xdist[psutil] responses jaraco-classes deepdiff httpretty" ;;
    *)
        echo "usage: derived_env.sh click|werkzeug|black|pytest|pydantic|astroid|poetry|sphinx [CHECKOUT]" >&2
        exit 2 ;;
esac

dir=$envs/$name
mkdir -p "$envs"
if [ ! -x "$dir/bin/python" ]; then
    echo "creating $dir" >&2
    "$python" -m venv "$dir"
fi
# No --upgrade: what an environment already holds stays as it is.
# shellcheck disable=SC2086
"$dir/bin/pip" install -q $specs >&2
# An environment built before this rule may hold the released project; hypothesis and the like do not need it.
if [ "$repo" = pytest ]; then "$dir/bin/pip" uninstall -y -q pytest >&2 || true; fi

wrapper=$dir/bin/pysrc
temporary=$wrapper.new.$$
printf '#!/bin/sh\nPYTHONPATH="$PWD/src:$PWD${PYTHONPATH:+:$PYTHONPATH}" exec %s/bin/python "$@"\n' "$dir" > "$temporary"
chmod +x "$temporary"
if cmp -s "$temporary" "$wrapper" 2>/dev/null; then rm -f "$temporary"; else mv "$temporary" "$wrapper"; fi
echo "$wrapper"
