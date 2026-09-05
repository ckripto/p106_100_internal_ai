"""SSH-backed tool state for keeping Executor workloads off the web container."""

import json
import shlex
import subprocess
import time
from pathlib import PurePosixPath

from agents.shared import ProtocolError, ToolState, clip


PREPARE_SCRIPT = """
import os
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
root.mkdir(parents=True, exist_ok=True)
os.chmod(root, 0o700)
"""

FILE_SCRIPT = """
import os
import sys
import tempfile
from pathlib import Path

operation, root_value, relative_value = sys.argv[1:4]
root = Path(root_value).resolve()
relative = Path(relative_value)
target = (root / relative).resolve()
if relative.is_absolute() or ".." in relative.parts or target == root or not target.is_relative_to(root):
    raise SystemExit("path outside remote workspace")

if operation == "read":
    offset, limit = map(int, sys.argv[4:6])
    with target.open(encoding="utf-8") as stream:
        stream.read(offset)
        sys.stdout.write(stream.read(limit))
elif operation == "write":
    target.parent.mkdir(parents=True, exist_ok=True)
    target = target.resolve()
    if not target.is_relative_to(root):
        raise SystemExit("path outside remote workspace")
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=target.parent, delete=False
        ) as stream:
            temporary_name = stream.name
            stream.write(sys.stdin.buffer.read())
        os.replace(temporary_name, target)
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)
else:
    raise SystemExit("unknown file operation")
"""

LIST_SCRIPT = """
import json
import os
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
excluded = set(json.loads(sys.argv[2]))
files = []
for directory, child_directories, names in os.walk(root):
    child_directories[:] = sorted(name for name in child_directories if name not in excluded)
    for name in sorted(names):
        path = Path(directory, name).resolve()
        if not path.is_relative_to(root):
            continue
        files.append(str(path.relative_to(root)))
        if len(files) == 41:
            break
    if len(files) == 41:
        break
sys.stdout.write(json.dumps({"success": True, "files": files[:40], "truncated": len(files) > 40}))
"""


class SSHToolState(ToolState):
    def _target(self):
        target = self.settings.ssh_target
        if (
            not isinstance(target, str)
            or not target
            or target.startswith("-")
            or any(character.isspace() for character in target)
        ):
            raise ValueError("EXECUTOR_SSH_TARGET is missing or invalid")
        return target

    def _remaining(self):
        remaining = self.settings.command_timeout
        if self.deadline is not None:
            remaining = min(remaining, self.deadline - time.monotonic())
        if remaining <= 0:
            raise OSError("Remote operation exceeded the agent deadline")
        return remaining

    def _ssh_arguments(self, remote_command):
        return [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", f"ConnectTimeout={self.settings.ssh_connect_timeout}",
            self._target(),
            remote_command,
        ]

    def _remote_python(self, script, *arguments, input_bytes=None):
        command = shlex.join(["python3", "-c", script, *map(str, arguments)])
        try:
            result = subprocess.run(
                self._ssh_arguments(command),
                input=input_bytes,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self._remaining(),
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise OSError("Remote SSH operation timed out") from None
        if result.returncode:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise OSError(clip(detail or f"SSH exited with code {result.returncode}", 300))
        return result.stdout.decode("utf-8", errors="strict")

    def prepare(self):
        self._remote_python(PREPARE_SCRIPT, self.settings.root)

    def path_key(self, filename):
        if not isinstance(filename, str) or not filename or len(filename) > 240:
            raise ProtocolError("Invalid path")
        path = PurePosixPath(filename)
        prefix = self.settings.redundant_path_prefix
        if prefix and path.parts[:1] == (prefix,):
            raise ProtocolError(
                f"Paths are already relative to {self.settings.root}; remove the {prefix}/ prefix"
            )
        if path.is_absolute() or not path.parts or ".." in path.parts:
            raise ProtocolError("Path must be inside the configured root")
        return path.as_posix()

    def _write_file(self, relative_path, content):
        self._remote_python(
            FILE_SCRIPT,
            "write",
            self.settings.root,
            relative_path,
            input_bytes=content.encode("utf-8"),
        )

    def _read_file(self, relative_path, offset):
        return self._remote_python(
            FILE_SCRIPT,
            "read",
            self.settings.root,
            relative_path,
            offset,
            self.settings.chunk_size + 1,
        )

    def _read_all(self, relative_path):
        content = self._remote_python(
            FILE_SCRIPT,
            "read",
            self.settings.root,
            relative_path,
            0,
            self.settings.staging_limit + 1,
        )
        if len(content) > self.settings.staging_limit:
            raise ProtocolError("File exceeds edit limit")
        return content

    def _list_files(self):
        output = self._remote_python(
            LIST_SCRIPT,
            self.settings.root,
            json.dumps(sorted(self.settings.excluded_names)),
        )
        try:
            result = json.loads(output)
        except ValueError:
            raise OSError("Remote file listing returned invalid JSON") from None
        if not isinstance(result, dict) or result.get("success") is not True:
            raise OSError("Remote file listing returned an invalid result")
        return result

    def _run_command(self, command):
        remaining = self._remaining()
        remote_limit = max(1, int(remaining) - 1)
        remote_command = (
            f"cd -- {shlex.quote(str(self.settings.root))} && "
            f"exec timeout --foreground --signal=KILL {remote_limit}s "
            f"/bin/bash -o pipefail -c {shlex.quote(command)}"
        )
        result = self._run_process(self._ssh_arguments(remote_command), None)
        if result["returncode"] in {124, 137}:
            result["success"] = False
            result["timed_out"] = True
        return result
