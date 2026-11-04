"""Bounded, cancellable subprocesses and anonymous Git; never invoke a shell."""
import os
from pathlib import Path
import re
import selectors
import signal
import shlex
import subprocess
import tempfile
import time

from .store import now


class Cancelled(Exception):
    pass


def clean_env(extra=None):
    names = ("PATH", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR")
    env = {key: os.environ[key] for key in names if key in os.environ}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null",
               GIT_CONFIG_COUNT="2", GIT_CONFIG_KEY_0="credential.helper", GIT_CONFIG_VALUE_0="",
               GIT_CONFIG_KEY_1="core.hooksPath", GIT_CONFIG_VALUE_1="/dev/null",
               DOTNET_CLI_TELEMETRY_OPTOUT="1", POWERSHELL_TELEMETRY_OPTOUT="1",
               CARGO_TERM_COLOR="never", PYTHONUNBUFFERED="1")
    env.update(extra or {})
    return env


def scrub(text):
    return re.sub(r"(?:github_pat_|gh[pousr]_)[A-Za-z0-9_]+", "[MASKIERT]", text)


class Context:
    def __init__(self, store, job):
        self.store, self.job = store, job
        self.log_path = store.state / "logs" / (job["id"] + ".log")
        self.log_path.parent.mkdir(exist_ok=True)
        self.last_check = 0

    def log(self, value):
        with self.log_path.open("a", encoding="utf-8") as stream:
            if stream.tell() < 50 * 1024**2:
                stream.write(scrub(str(value)))
                if not str(value).endswith("\n"):
                    stream.write("\n")

    def phase(self, value):
        self.check()
        if self.job['kind'] == 'sdk_select':
            self.progress('working', value)
        else:
            self.store.change_job(self.job["id"], phase=scrub(value))
        self.store.set("worker", {"heartbeat": time.time(), "job": self.job["id"]})
        self.log(now() + "  " + value)

    def check(self):
        if getattr(self, 'activation_committed', False):
            return
        if self.store.job(self.job["id"])["cancel"]:
            raise Cancelled("Job cancelled.")

    def progress(self, stage, message, *, downloaded=None, total=None, percent=None, scope=None):
        if self.job['kind'] not in ('sdk_select','app_download','app_update') or getattr(self, 'activation_committed', False):
            return
        self.check()
        message = scrub(re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', str(message)))
        message = ' '.join(message.split())[:1000]
        if total and downloaded is not None:
            percent = min(100, max(0, downloaded*100/total))
        progress = {'stage': stage, 'message': message, 'scope': scope,
                    'downloaded_bytes': downloaded, 'total_bytes': total,
                    'percent': percent, 'indeterminate': percent is None}
        result = self.store.job(self.job['id'])['result']
        result['progress'] = progress
        self.store.change_job(self.job['id'], phase=message, result=result)
        self.store.set('worker', {'heartbeat': time.time(), 'job': self.job['id']})

    def tool_output(self, text):
        if self.job['kind'] != 'sdk_select' or time.monotonic()-getattr(self, '_last_output', 0) < .5:
            return
        text = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', text)
        # curl's own progress meter is measurable, unlike a generic setup phase.
        measured = re.findall(r'#+[^\r\n]*?\b(\d{1,3}\.\d+)%', text)
        if measured:
            self.progress('downloading', 'Downloading SDK tool archive', percent=min(100, float(measured[-1])), scope='Current tool archive')
        else:
            lines = [line.strip() for line in text.splitlines() if line.strip() and line.strip()[0] not in '{}"#']
            if lines:
                self.progress('setup', lines[-1])
        self._last_output = time.monotonic()

    def run(self, arguments, cwd=None, *, env=None, timeout=3600, check=True, quiet=False):
        self.check()
        args = [str(item) for item in arguments]
        if not quiet:
            self.log("$ " + " ".join(args))
        runtime_env = {"ZIG_GLOBAL_CACHE_DIR": str(self.store.root / "BuildTools/Cache/Zig/Global"),
                       "ZIG_LOCAL_CACHE_DIR": str(self.store.root / "BuildTools/Cache/Zig/Local"),
                       "XDG_CACHE_HOME": str(self.store.root / "BuildTools/Cache/Host"),
                       "DOTNET_CLI_HOME": str(self.store.root / "home")}
        runtime_env.update(env or {})
        process = subprocess.Popen(args, cwd=cwd, env=clean_env(runtime_env), stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        output, size = [], 0
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline, heartbeat = time.monotonic() + timeout, 0
        try:
            while selector.get_map() or process.poll() is None:
                self.check()
                if time.monotonic() > deadline:
                    raise RuntimeError("The operation timed out.")
                if time.monotonic() - heartbeat > 5:
                    self.store.set("worker", {"heartbeat": time.time(), "job": self.job["id"]})
                    heartbeat = time.monotonic()
                for key, _ in selector.select(.3):
                    chunk = os.read(key.fileobj.fileno(), 16384)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    text = chunk.decode("utf-8", "replace")
                    if not quiet:
                        self.log(text)
                        self.tool_output(text)
                    if size < 8 * 1024**2:
                        output.append(text)
                        size += len(chunk)
            code = process.wait(timeout=10)
            text = "".join(output)
            if code and check:
                readable = re.sub(r"\x1b\[[0-9;]*m", "", text)
                compiler_error = re.search(r"error\[[A-Z0-9]+\]:.{0,2500}", readable)
                detail = compiler_error.group(0).split('{"reason":', 1)[0] if compiler_error else readable[-4000:]
                raise RuntimeError(scrub(detail or f"Befehl fehlgeschlagen ({code}): {args[0]}"))
            return text, code
        finally:
            selector.close()
            # The process-group leader may have exited while descendants are
            # still running or holding the output pipe open.
            def group_signal(value):
                try:
                    os.killpg(process.pid, value)
                    return True
                except ProcessLookupError:
                    return False

            if group_signal(signal.SIGTERM):
                deadline = time.monotonic() + 3
                while group_signal(0) and time.monotonic() < deadline:
                    process.poll()
                    time.sleep(.03)
                if group_signal(0):
                    group_signal(signal.SIGKILL)
                    deadline = time.monotonic() + 1
                    while group_signal(0) and time.monotonic() < deadline:
                        process.poll()
                        time.sleep(.03)
            process.wait()
            process.stdout.close()

    def git(self, root, *args, **kwargs):
        return self.run(["git", "-C", root, *args], quiet=True, timeout=120, **kwargs)

    def repository_git(self, repo, *args, **kwargs):
        """Scope private network credentials to one validated repository/call."""
        if not repo.get("private"):
            return self.run(["git", *args], **kwargs)
        from . import connections
        repo = self.store.require_repository(repo["name"])
        connection = connections.get(self.store, repo["connection_id"])
        expected = "https://github.com/" + connection["owner"] + "/" + repo["display_name"] + ".git"
        if repo["clone_url"] != expected or repo["owner"].casefold() != connection["owner"].casefold():
            raise ValueError("The private repository does not match its connection.")
        # No secret is copied into arguments, environment values or helper files.
        with tempfile.TemporaryDirectory(prefix=".git-auth-", dir=self.store.state) as folder:
            helper = Path(folder) / "askpass"
            helper.write_text("#!/bin/sh\nexec /usr/bin/python3 -I -B " + shlex.quote(str(Path(__file__).with_name("git_askpass.py"))) + ' "$@"\n')
            helper.chmod(0o700)
            env = dict(kwargs.pop("env", {}) or {}, GIT_ASKPASS=str(helper),
                       R4OS_GIT_URL=expected, R4OS_GIT_CREDENTIAL_FILE=str(connections.secret_path(self.store, connection["id"])))
            try:
                return self.run(["git", "-c", "credential.useHttpPath=true", "-c", "http.followRedirects=false",
                                 "-c", "protocol.allow=never", "-c", "protocol.https.allow=always", *args], env=env, **kwargs)
            except (OSError, RuntimeError):
                with self.store.connect() as db:
                    db.execute("UPDATE repositories SET metadata=json_set(metadata,'$.read_access',json('false'),'$.access_error',?) WHERE name=?",
                               ("Private source access failed. Check the connection token and refresh its catalogue.", repo["name"]))
                raise
