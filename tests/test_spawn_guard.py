"""Every process conductor starts passes an explicit, scrubbed environment.

An AST guard walks every module under ``src/conductor``: a ``subprocess`` or ``asyncio`` spawn without ``env=``
fails the build, and so do ``os.system``, ``os.popen``, ``os.startfile`` and ``webbrowser.open``, which take no
environment at all. Keyword dicts passed as ``**kwargs`` count only when every binding of the name in the call's own
scope is a dict literal naming ``env``; anything the guard cannot see fails closed.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from conductor.procs import SECRET_NAMES, SECRET_PREFIXES, SECRET_SUFFIXES, scrubbed_env

SOURCE = Path(__file__).resolve().parents[1] / "src" / "conductor"


def spawn_violations(source: str, filename: str) -> list[str]:
    tree = ast.parse(source)
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                aliases[item.asname or item.name.split(".")[0]] = item.name if item.asname else item.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            for item in node.names:
                aliases[item.asname or item.name] = f"{node.module}.{item.name}"

    def name(node) -> str:
        if isinstance(node, ast.Name):
            return aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            return f"{name(node.value)}.{node.attr}"
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            return f"{name(node.args[0])}.{node.args[1].value}"
        return ""

    scopes = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}

    def enclosing(node):
        while node in parents:
            node = parents[node]
            if isinstance(node, scopes):
                return node
        return tree

    def assigned(scope, target: str) -> list:
        """Every binding of ``target`` in ``scope``: the value of a plain ``name = value``, else None (unknown).

        A parameter, a global or nonlocal, an import, a loop, ``with`` or ``except`` target, an unpacking or a
        walrus binds the name to something this guard cannot see, so it counts as None. So does any other use of the
        name (``kw.clear()``, ``del kw["env"]``, passing it elsewhere): only the ``**`` expansion and setting a key
        other than ``env`` leave the literal's ``env`` provably in place.
        """
        values: list = []
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            arguments = scope.args
            values += [
                None
                for arg in (
                    *arguments.posonlyargs,
                    *arguments.args,
                    *arguments.kwonlyargs,
                    arguments.vararg,
                    arguments.kwarg,
                )
                if arg is not None and arg.arg == target
            ]
        stack = list(ast.iter_child_nodes(scope))
        while stack:
            node = stack.pop()
            if isinstance(node, scopes):
                values += [None] if getattr(node, "name", None) == target else []
                if any(isinstance(inner, ast.Name) and inner.id == target for inner in ast.walk(node)):
                    values.append(None)  # A captured dictionary can be mutated when this nested scope runs.
                continue
            if isinstance(node, ast.Name) and node.id == target and isinstance(node.ctx, (ast.Store, ast.Del)):
                parent = parents.get(node)
                plain = isinstance(parent, ast.Assign) and node in parent.targets
                values.append(parent.value if plain else None)
            elif unseen(node, target):
                values.append(None)
            stack.extend(ast.iter_child_nodes(node))
        return values

    def harmless_use(node: ast.Name) -> bool:
        """The ``**`` expansion itself, or ``kw["key"] = value`` for a key other than ``env``."""
        parent = parents.get(node)
        if isinstance(parent, ast.keyword) and parent.arg is None:
            return True
        return (
            isinstance(parent, ast.Subscript)
            and isinstance(parent.ctx, ast.Store)
            and isinstance(parent.slice, ast.Constant)
            and parent.slice.value != "env"
        )

    def unseen(node, target: str) -> bool:
        """A use of ``target`` that may change what it holds, or a binding this guard cannot see into."""
        if isinstance(node, ast.Name) and node.id == target:
            return not harmless_use(node)
        return binds_unseen(node, target)

    def binds_unseen(node, target: str) -> bool:
        """A global or nonlocal declaration, an import, or an ``except ... as`` that binds ``target``."""
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            return target in node.names
        if isinstance(node, ast.alias):
            return (node.asname or node.name.split(".")[0]) == target
        return isinstance(node, ast.ExceptHandler) and node.name == target

    def names_env(value) -> bool:
        return isinstance(value, ast.Dict) and any(isinstance(k, ast.Constant) and k.value == "env" for k in value.keys)

    def passes_env(call: ast.Call) -> bool:
        for keyword in call.keywords:
            if keyword.arg == "env":
                return True
            if keyword.arg is None and isinstance(keyword.value, ast.Name):
                values = assigned(enclosing(call), keyword.value.id)
                if values and all(names_env(value) for value in values):
                    return True
        return False

    spawns = {f"subprocess.{n}" for n in ("run", "Popen", "call", "check_call", "check_output")}
    spawns.update({"asyncio.create_subprocess_exec", "asyncio.create_subprocess_shell"})
    forbidden = {"os.system", "os.popen", "os.startfile", "webbrowser.open"}
    # Annotations name types, never call them: ``-> subprocess.Popen`` is no spawn.
    annotations = {
        id(inner)
        for node in ast.walk(tree)
        for annotation in (
            [node.returns] + [a.annotation for a in ast.walk(node.args) if isinstance(a, ast.arg)]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            else [node.annotation]
            if isinstance(node, ast.AnnAssign)
            else []
        )
        if annotation is not None
        for inner in ast.walk(annotation)
    }
    failures = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            target = name(node.func)
            if target in forbidden or (target in spawns and not passes_env(node)):
                failures.append(f"{filename}:{node.lineno}: {target}")
            if name(node) in spawns | forbidden:
                failures.append(f"{filename}:{node.lineno}: {name(node)} obtained indirectly")
        elif isinstance(node, (ast.Name, ast.Attribute)) and id(node) not in annotations:
            # A spawn function used as a value (``launch = subprocess.Popen``, ``partial(subprocess.run, ...)``)
            # can be called where this guard cannot see its keywords: refused outright.
            parent = parents.get(node)
            called = isinstance(parent, ast.Call) and parent.func is node
            target = name(node)
            if not called and (target in spawns or target in forbidden):
                failures.append(f"{filename}:{node.lineno}: {target} used as a value")
    return failures


def test_every_spawn_site_in_conductor_passes_env():
    paths = sorted(SOURCE.rglob("*.py"))
    assert len(paths) > 10
    failures = []
    for path in paths:
        failures.extend(spawn_violations(path.read_text(encoding="utf-8"), path.relative_to(SOURCE).as_posix()))
    assert failures == []


@pytest.mark.parametrize(
    "module,call",
    [("subprocess", n) for n in ("run", "Popen", "call", "check_call", "check_output")]
    + [("asyncio", "create_subprocess_exec"), ("asyncio", "create_subprocess_shell")]
    + [("os", "system"), ("os", "popen"), ("os", "startfile"), ("webbrowser", "open")],
)
@pytest.mark.parametrize("direct", [False, True])
def test_guard_detects_aliases(module, call, direct):
    source = (
        f"from {module} import {call} as launch\nlaunch('probe')"
        if direct
        else f"import {module} as proc\nproc.{call}('probe')"
    )
    assert spawn_violations(source, "fixture.py") == [f"fixture.py:2: {module}.{call}"]


def test_guard_allows_explicit_env():
    assert not spawn_violations("from subprocess import run as r\nr(['echo'], env={})", "fixture.py")
    assert not spawn_violations("import subprocess\nkw = {'env': {}}\nsubprocess.Popen(['echo'], **kw)", "fixture.py")
    assert spawn_violations("import subprocess\nkw = {'cwd': '.'}\nsubprocess.Popen(['echo'], **kw)", "fixture.py") == [
        "fixture.py:3: subprocess.Popen"
    ]


@pytest.mark.parametrize(
    "source",
    [
        # Another function's dict must not certify this call.
        "import subprocess\ndef unsafe():\n    kw = {}\n    subprocess.run(['probe'], **kw)\n"
        "def unrelated():\n    kw = {'env': {}}\n",
        "import subprocess\nkw = {'env': {}}\ndef unsafe():\n    subprocess.run(['probe'], **kw)\n",
        "import subprocess\ndef unsafe(kw):\n    subprocess.run(['probe'], **kw)\n",
        "import subprocess\ndef unsafe():\n    kw = {'env': {}}\n    kw = dict(cwd='.')\n"
        "    subprocess.run(['probe'], **kw)\n",
        # A parameter replaced only on one branch.
        "import subprocess\ndef unsafe(kw, replace=False):\n    if replace:\n        kw = {'env': {}}\n"
        "    subprocess.run(['probe'], **kw)\n",
        "import subprocess\ndef unsafe(*, kw):\n    kw = kw or {'env': {}}\n    subprocess.run(['probe'], **kw)\n",
        "import subprocess\ndef unsafe(items):\n    kw = {'env': {}}\n    for kw in items:\n"
        "        subprocess.run(['probe'], **kw)\n",
        "import subprocess\ndef unsafe(pairs):\n    kw = {'env': {}}\n    kw, other = pairs\n"
        "    subprocess.run(['probe'], **kw)\n",
        "import subprocess\ndef unsafe(make):\n    kw = {'env': {}}\n    with make() as kw:\n"
        "        subprocess.run(['probe'], **kw)\n",
        "import subprocess\nkw = {'env': {}}\ndef unsafe():\n    global kw\n    subprocess.run(['probe'], **kw)\n",
        "import subprocess\ndef unsafe(get):\n    kw = {'env': {}}\n    if (kw := get()):\n"
        "        subprocess.run(['probe'], **kw)\n",
    ],
)
def test_guard_resolves_keyword_dicts_in_the_calls_own_scope(source):
    assert spawn_violations(source, "fixture.py") != []


@pytest.mark.parametrize(
    "source",
    [
        # A literal naming env, then emptied before the expansion.
        "import subprocess\nkw = {'env': {}}\nkw.clear()\nsubprocess.run(['probe'], **kw)\n",
        "import subprocess\nkw = {'env': {}}\ndel kw['env']\nsubprocess.run(['probe'], **kw)\n",
        "import subprocess\nkw = {'env': {}}\nkw['env'] = None\nsubprocess.run(['probe'], **kw)\n",
        "import subprocess\ndef unsafe(change):\n    kw = {'env': {}}\n    change(kw)\n"
        "    subprocess.run(['probe'], **kw)\n",
        # A spawn function stored or passed as a value.
        "import subprocess\nlaunch = subprocess.Popen\nlaunch(['probe'])\n",
        "import functools, subprocess\nrun = functools.partial(subprocess.run, check=True)\nrun(['probe'])\n",
        "from subprocess import run as r\nlaunchers = [r]\nlaunchers[0](['probe'])\n",
    ],
)
def test_guard_refuses_mutated_keyword_dicts_and_stored_spawn_functions(source):
    assert spawn_violations(source, "fixture.py") != []


@pytest.mark.parametrize(
    "source",
    [
        "import subprocess\ndef launch():\n    kw = {'env': {}}\n"
        "    def clear_options():\n        kw.clear()\n    clear_options()\n    subprocess.run(['probe'], **kw)\n",
        "import subprocess\ndef launch():\n    kw = {'env': {}}\n"
        "    clear = lambda: kw.clear()\n    clear()\n    subprocess.run(['probe'], **kw)\n",
        "import subprocess\nsubprocess.run.__call__(['probe'])\n",
        "import subprocess\ngetattr(subprocess, 'run')(['probe'])\n",
        "import subprocess\nlaunch = getattr(subprocess, 'run')\nlaunch(['probe'])\n",
        "from subprocess import run\ngetattr(run, '__call__')(['probe'])\n",
    ],
)
def test_guard_refuses_closure_mutations_and_indirect_spawn_attributes(source):
    assert spawn_violations(source, "fixture.py")


def test_guard_ignores_annotations():
    source = (
        "import subprocess\ndef spawn(argv, env) -> subprocess.Popen:\n    return subprocess.Popen(argv, env=env)\n"
        "def stop(process: subprocess.Popen) -> None:\n    pass\nhandle: subprocess.Popen | None = None\n"
    )
    assert spawn_violations(source, "fixture.py") == []


def test_guard_accepts_a_keyword_dict_built_beside_the_call():
    source = (
        "import subprocess\ndef safe(env):\n    kw = {'cwd': '.', 'env': env}\n    kw['shell'] = False\n"
        "    subprocess.Popen(['probe'], **kw)\n"
    )
    assert spawn_violations(source, "fixture.py") == []


def test_scrub_rules_are_case_insensitive_and_pure():
    secrets = {name.lower(): "fake" for name in SECRET_NAMES}
    secrets.update({prefix.lower() + "probe": "fake" for prefix in SECRET_PREFIXES})
    secrets.update({"probe" + suffix.lower(): "fake" for suffix in SECRET_SUFFIXES})
    secrets.update({"GIT_DIR": "elsewhere", "git_work_tree": "elsewhere", "git_askpass": "helper"})
    startup = {name: "kept" for name in ("PATH", "HOME", "SYSTEMROOT", "TEMP", "LANG", "USERPROFILE")}
    inherited = {**startup, **secrets, "unrelated_setting": "not on the allowlist"}
    original = dict(inherited)
    assert scrubbed_env(inherited) == startup
    assert scrubbed_env(inherited, {"CONDUCTOR_HOME": "x", "EXTRA": "y"}) == {**startup, "EXTRA": "y"}
    # Listing a secret-shaped name in a workflow does not bring it back; only an adapter's allow does.
    assert scrubbed_env(inherited, extend=list(secrets)) == startup
    assert inherited == original


def test_scrub_allow_is_an_explicit_opt_in():
    inherited = {"PATH": "p", "ANTHROPIC_API_KEY": "fake", "OPENAI_API_KEY": "fake"}
    assert scrubbed_env(inherited, allow=["anthropic_api_key"]) == {"PATH": "p", "ANTHROPIC_API_KEY": "fake"}
