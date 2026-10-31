"""Child processes: argv lists, no shell, an allow-listed environment, a timeout, and a stop that reaches the tree.

Every process conductor starts passes an explicit ``env=``; ``tests/test_spawn_guard.py`` fails the build on a
spawn site that does not. The environment comes from :func:`scrubbed_env`: only the variables named in
``BASE_ALLOW`` (and the ``LC_*`` and ``XDG_*`` families) are inherited, a workflow can add names, and anything that
looks like a secret is dropped even then. An adapter that needs its own credential names it (``allow``).

This governs the environment only. A child still runs as the operator's user: files such as ``~/.ssh``, git's
credential helpers and the agent CLIs' own login files stay readable to it unless it runs sandboxed.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from collections.abc import Iterable, Mapping, Sequence

# What a child inherits: enough to find programs, a home, a temporary directory and a locale, on POSIX and Windows.
BASE_ALLOW = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "LANG",
        "LANGUAGE",
        "TZ",
        "TERM",
        "COLORTERM",
        "NO_COLOR",
        "TMPDIR",
        "TEMP",
        "TMP",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "PYTHONUTF8",
        "PYTHONIOENCODING",
        "PYTHONDONTWRITEBYTECODE",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "OS",
        "USERPROFILE",
        "USERNAME",
        "HOMEDRIVE",
        "HOMEPATH",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMDATA",
        "ALLUSERSPROFILE",
        "PUBLIC",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "PROGRAMW6432",
        "COMMONPROGRAMFILES",
        "COMMONPROGRAMFILES(X86)",
        "COMMONPROGRAMW6432",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
        "PROCESSOR_IDENTIFIER",
    }
)
BASE_ALLOW_PREFIXES = ("LC_", "XDG_")
# Dropped even when a workflow or an extra value names them. GIT_CONFIG_* is environment-supplied git configuration
# (GIT_CONFIG_COUNT, GIT_CONFIG_KEY_n, GIT_CONFIG_VALUE_n, GIT_CONFIG_PARAMETERS): the usual way a credential helper
# or an extra header reaches git. CONDUCTOR_* keeps the grant record and the policy file out of a child's view.
SECRET_NAMES = frozenset(
    {
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GITLAB_TOKEN",
        "NPM_TOKEN",
        "PYPI_TOKEN",
        "TWINE_PASSWORD",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AZURE_CLIENT_ID",
        "AZURE_CLIENT_SECRET",
        "AZURE_TENANT_ID",
        "SSH_AUTH_SOCK",  # the ssh-agent: whoever reaches it can sign with the operator's keys
    }
)
SECRET_PREFIXES = ("CONDUCTOR_", "ANTHROPIC_", "OPENAI_", "CLAUDE_CODE_USE_", "GIT_CONFIG_")
SECRET_SUFFIXES = ("_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_CREDENTIALS", "_JWT", "_DSN")
SECRET_PARTS = ("ASKPASS",)  # GIT_ASKPASS, SSH_ASKPASS, SUDO_ASKPASS: programs that hand out passwords
# Variables that point git at another repository. A child that inherits them works on the wrong tree.
GIT_OVERRIDES = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_NAMESPACE",
    }
)


def is_secret(name: str) -> bool:
    upper = str(name).upper()
    return (
        upper in SECRET_NAMES
        or upper.startswith(SECRET_PREFIXES)
        or upper.endswith(SECRET_SUFFIXES)
        or any(part in upper for part in SECRET_PARTS)
    )


def inheritable(name: str, extend: Iterable[str] = ()) -> bool:
    upper = str(name).upper()
    return upper in BASE_ALLOW or upper.startswith(BASE_ALLOW_PREFIXES) or upper in {str(n).upper() for n in extend}


def scrubbed_env(
    inherited: Mapping[str, str],
    extra: Mapping[str, str] | None = None,
    *,
    extend: Sequence[str] = (),
    allow: Iterable[str] = (),
) -> dict[str, str]:
    """The environment a child starts with. Pure: neither mapping is changed.

    From ``inherited``, only ``BASE_ALLOW`` names, the ``LC_*`` and ``XDG_*`` families, and the names in ``extend``
    (a workflow's ``environment.env``). ``extra`` holds values conductor sets itself and is kept as given. Anything
    secret-shaped, from either, is dropped, unless it is named in ``allow``: an adapter's explicit, documented
    opt-in for its own credential. Git overrides are always dropped.
    """
    allowed = {str(name).upper() for name in allow}
    extended = [str(name).upper() for name in extend]
    clean: dict[str, str] = {}
    for source, filtered in ((inherited, True), (extra or {}, False)):
        for raw_name, raw_value in source.items():
            name = str(raw_name)
            upper = name.upper()
            if upper in GIT_OVERRIDES:
                continue
            if upper not in allowed and (is_secret(upper) or (filtered and not inheritable(upper, extended))):
                continue
            clean[name] = str(raw_value)
    return clean


def group_options() -> dict:
    """Start the child as the leader of its own process group, so a stop reaches what it starts."""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def spawn(
    argv: Sequence[str], *, cwd: str, env: Mapping[str, str], stdout=None, stderr=None, input_text: str | None = None
) -> subprocess.Popen:
    """A child with no shell, in its own process group, that can never answer a prompt.

    Its stdin is empty, or ``input_text`` written once and closed (an agent's brief): either way it reaches the end
    of input, and nothing it asks for is ever typed.
    """
    process = subprocess.Popen(
        [str(part) for part in argv],
        cwd=cwd,
        env=dict(env),
        stdin=subprocess.DEVNULL if input_text is None else subprocess.PIPE,
        stdout=stdout,
        stderr=stderr,
        shell=False,
        **group_options(),
    )
    if input_text is not None:
        threading.Thread(target=_feed, args=(process, input_text.encode("utf-8")), daemon=True).start()
    return process


def _feed(process: subprocess.Popen, data: bytes) -> None:
    try:
        process.stdin.write(data)
    except OSError:
        pass  # the child exited without reading its input
    finally:
        try:
            process.stdin.close()
        except OSError:
            pass


def stop_tree(process: subprocess.Popen, grace: float = 3.0) -> None:
    """Stop a child started by :func:`spawn` and the processes it started. Never raises.

    POSIX signals the whole process group: SIGTERM, then SIGKILL after ``grace``. Windows sends CTRL_BREAK to the
    child's group, then terminates it; grandchildren are reached when the optional ``psutil`` is installed.
    """
    if os.name == "nt":
        _stop_windows(process, grace)
        return
    _signal_group(process, signal.SIGTERM)
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    # The leader may have exited while its children live on: the group is signalled either way.
    _signal_group(process, signal.SIGKILL)
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass


def _signal_group(process: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(process.pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _stop_windows(process: subprocess.Popen, grace: float) -> None:  # pragma: no cover - exercised on Windows
    children = []
    try:
        import psutil  # optional

        children = psutil.Process(process.pid).children(recursive=True)
    except Exception:
        children = []
    if process.poll() is None:
        try:
            process.send_signal(signal.CTRL_BREAK_EVENT)
        except (OSError, ValueError):
            pass
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass
    for child in children:
        try:
            child.kill()
        except Exception:
            pass
    if process.poll() is None:
        try:
            process.kill()
            process.wait(timeout=grace)
        except (OSError, subprocess.TimeoutExpired):
            pass
