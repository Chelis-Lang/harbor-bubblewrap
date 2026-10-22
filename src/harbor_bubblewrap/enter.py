"""Run one command inside the namespaces of a running bubblewrap sandbox.

The bubblewrap backend starts one long-lived sandbox for each service. It runs
each later command with this program, as ``runc exec`` does for a container.
The program reads one JSON request on standard input. It joins each namespace
of the sandbox process, drops every capability, takes the sandbox user and
group IDs, and executes the command. It exits with the status of the command.

The program copies the output of the command and returns when the command
exits, as ``docker exec`` does. A background process of the command can keep
the output pipes open, so the program does not wait for them to close.

The program uses only the standard library, so the backend runs it by path
with ``python3 -I``. Linux refuses to join a user namespace from a process
with more than one thread, so the program must stay single-threaded.

Request fields: ``pid`` (host PID), ``pidfd`` (its inherited process descriptor),
``uid``, ``gid``, ``cwd``, ``env`` and ``argv``.
"""

from __future__ import annotations

import ctypes
import fcntl
import json
import os
import select
import signal
import sys

# Namespaces to join after the user namespaces. bubblewrap makes a second,
# nested user namespace when the sandbox user ID is not 0. Its mount, PID and
# other namespaces then belong to the outer user namespace. So this program
# joins each namespace while it is in the user namespace that owns it.
NAMESPACES = (
    ("mnt", os.CLONE_NEWNS),
    ("ipc", os.CLONE_NEWIPC),
    ("uts", os.CLONE_NEWUTS),
    ("net", os.CLONE_NEWNET),
    ("cgroup", os.CLONE_NEWCGROUP),
    ("pid", os.CLONE_NEWPID),
)
NS_GET_USERNS = 0xB701
NS_GET_PARENT = 0xB702
PR_CAPBSET_DROP = 24
PR_SET_NO_NEW_PRIVS = 38
PR_CAP_AMBIENT = 47
PR_CAP_AMBIENT_CLEAR_ALL = 4
CAPABILITY_VERSION_3 = 0x20080522
FAILURE = 127


class CapabilityHeader(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]


class CapabilityData(ctypes.Structure):
    _fields_ = [
        ("effective", ctypes.c_uint32),
        ("permitted", ctypes.c_uint32),
        ("inheritable", ctypes.c_uint32),
    ]


def _checked(result: int, call: str) -> None:
    if result != 0:
        number = ctypes.get_errno()
        raise OSError(number, f"{call}: {os.strerror(number)}")


def _inode(descriptor: int) -> int:
    return os.fstat(descriptor).st_ino


def _user_chain(pid: int) -> list[int]:
    """Return the user namespaces of ``pid`` below this process, outermost first."""
    own = os.stat("/proc/self/ns/user").st_ino
    chain = []
    current = os.open(f"/proc/{pid}/ns/user", os.O_RDONLY)
    while _inode(current) != own:
        chain.append(current)
        current = fcntl.ioctl(current, NS_GET_PARENT)
    os.close(current)
    chain.reverse()
    return chain


def _other_namespaces(pid: int) -> list[tuple[int, int, int]]:
    """Open each namespace of ``pid`` that differs from the namespace of this process.

    Each row holds the descriptor, the clone flag and the inode of the owner
    user namespace. A sandbox can share a namespace with the host, for example
    the network of a public task. This process stays in such a namespace.
    """
    opened = []
    for name, flag in NAMESPACES:
        target = f"/proc/{pid}/ns/{name}"
        own = (
            "/proc/self/ns/pid_for_children"
            if name == "pid"
            else f"/proc/self/ns/{name}"
        )
        if os.stat(target).st_ino == os.stat(own).st_ino:
            continue
        descriptor = os.open(target, os.O_RDONLY)
        owner = fcntl.ioctl(descriptor, NS_GET_USERNS)
        opened.append((descriptor, flag, _inode(owner)))
        os.close(owner)
    return opened


def _join(pid: int, descriptor: int) -> None:
    pending = _other_namespaces(pid)
    users = _user_chain(pid)
    # Validate identity after all /proc opens, before any namespace change.
    signal.pidfd_send_signal(descriptor, 0)
    for user in users:
        os.setns(user, os.CLONE_NEWUSER)
        current = _inode(user)
        os.close(user)
        for row in [row for row in pending if row[2] == current]:
            os.setns(row[0], row[1])
            os.close(row[0])
            pending.remove(row)
    if pending:
        raise RuntimeError(
            f"process {pid} has namespaces outside its own user namespaces"
        )


def _drop_privileges(
    libc: ctypes.CDLL,
    uid: int,
    gid: int,
    last_capability: int,
    *,
    capabilities: int = 0,
    no_new_privs: bool = True,
    clear_groups: bool = False,
) -> None:
    if capabilities < 0 or capabilities >> (last_capability + 1):
        raise ValueError("capability mask exceeds the kernel capability set")
    if uid != 0 and capabilities:
        raise ValueError("non-root commands cannot retain capabilities")
    for capability in range(last_capability + 1):
        if not capabilities & (1 << capability):
            _checked(
                libc.prctl(PR_CAPBSET_DROP, capability, 0, 0, 0), "PR_CAPBSET_DROP"
            )
    _checked(
        libc.prctl(PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL, 0, 0, 0),
        "PR_CAP_AMBIENT_CLEAR_ALL",
    )
    if clear_groups:
        os.setgroups([])
    os.setresgid(gid, gid, gid)
    os.setresuid(uid, uid, uid)
    header = CapabilityHeader(CAPABILITY_VERSION_3, 0)
    data = (CapabilityData * 2)()
    for index in range(2):
        bits = (capabilities >> (32 * index)) & 0xFFFFFFFF
        data[index].effective = bits
        data[index].permitted = bits
    _checked(libc.capset(ctypes.byref(header), data), "capset")
    if no_new_privs:
        _checked(libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0), "PR_SET_NO_NEW_PRIVS")


def _program(name: str, env: dict) -> str:
    """Find ``name`` on the PATH of ``env``.

    After chroot, the host Python library is out of view, so this function
    imports nothing. ``os.execvpe`` imports ``warnings`` when it searches PATH.
    """
    if "/" in name:
        return name
    for directory in env.get("PATH", "").split(":"):
        candidate = os.path.join(directory or ".", name)
        if os.access(candidate, os.X_OK) and not os.path.isdir(candidate):
            return candidate
    raise FileNotFoundError(f"{name} is not on PATH")


def _child(request: dict, root: int, null: int, last_capability: int) -> None:
    """Enter the sandbox root and execute the command. Never returns."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        os.fchdir(root)
        os.chroot(".")
        os.chdir(request["cwd"])
        _drop_privileges(
            libc,
            int(request["uid"]),
            int(request["gid"]),
            last_capability,
            capabilities=int(request.get("capabilities", 0)),
            no_new_privs=request.get("no_new_privs", True),
            clear_groups=request.get("mapped_ids", False),
        )
        os.dup2(request.get("stdin_fd", null), 0)
        for name in ("stdin_fd", "started_fd"):
            descriptor = request.get(name)
            if descriptor is not None and descriptor > 2:
                os.close(descriptor)
        argv = request["argv"]
        os.execve(_program(argv[0], request["env"]), argv, request["env"])
    except BaseException as error:  # noqa: BLE001 - report the cause, then exit.
        if "exec_status_fd" in request:
            os.write(request["exec_status_fd"], b"failed")
        os.write(2, f"enter: {type(error).__name__}: {error}\n".encode())
    os._exit(FAILURE)


def _write_all(descriptor: int, data: bytes) -> None:
    while data:
        data = data[os.write(descriptor, data) :]


def _copy(source: int, streams: dict[int, int]) -> bool:
    """Copy one chunk from ``source``. Return False at end of file or with no data."""
    try:
        data = os.read(source, 65536)
    except BlockingIOError:
        return False
    if not data:
        del streams[source]
        return False
    _write_all(streams[source], data)
    return True


def _relay(child: int, streams: dict[int, int]) -> int:
    """Copy output until ``child`` exits, then copy the output left in the pipes."""
    exited = os.pidfd_open(child)
    while True:
        readable, _, _ = select.select([*streams, exited], [], [])
        for source in readable:
            if source in streams:
                _copy(source, streams)
        if exited in readable:
            break
    for source in list(streams):
        os.set_blocking(source, False)
        while source in streams and _copy(source, streams):
            pass
    _, status = os.waitpid(child, 0)
    return status


def bootstrap() -> int:
    """Set the mapped role identity before the first service command."""
    uid, gid, capabilities, no_new_privs = map(int, sys.argv[2:6])
    argv = json.loads(sys.argv[6])
    with open("/proc/sys/kernel/cap_last_cap") as file:
        last_capability = int(file.read())
    _drop_privileges(
        ctypes.CDLL(None, use_errno=True),
        uid,
        gid,
        last_capability,
        capabilities=capabilities,
        no_new_privs=bool(no_new_privs),
        clear_groups=True,
    )
    environment = dict(os.environ)
    os.execve(_program(argv[0], environment), argv, environment)


def main() -> int:
    request = json.load(sys.stdin)
    pid = int(request["pid"])
    with open("/proc/sys/kernel/cap_last_cap") as file:
        last_capability = int(file.read())
    null = os.open("/dev/null", os.O_RDONLY)
    root = os.open(f"/proc/{pid}/root", os.O_RDONLY | os.O_DIRECTORY)
    descriptor = int(request["pidfd"])
    _join(pid, descriptor)
    os.close(descriptor)
    out_read, out_write = os.pipe()
    err_read, err_write = os.pipe()
    status_read, status_write = (
        os.pipe2(os.O_CLOEXEC) if "started_fd" in request else (None, None)
    )
    # A new PID namespace applies only to children, so fork once.
    child = os.fork()
    if child == 0:
        if status_write is not None:
            os.close(status_read)
            request["exec_status_fd"] = status_write
        os.dup2(out_write, 1)
        os.dup2(err_write, 2)
        for descriptor in (out_read, out_write, err_read, err_write):
            os.close(descriptor)
        _child(request, root, null, last_capability)
    os.close(out_write)
    os.close(err_write)
    if status_read is not None:
        os.close(status_write)
        failure = os.read(status_read, 64)
        os.close(status_read)
        if not failure:
            os.write(request["started_fd"], json.dumps({"pid": child}).encode())
        os.close(request["started_fd"])
    status = _relay(child, {out_read: 1, err_read: 2})
    code = os.waitstatus_to_exitcode(status)
    return 128 - code if code < 0 else code


if __name__ == "__main__":
    sys.exit(
        bootstrap() if len(sys.argv) > 1 and sys.argv[1] == "--bootstrap" else main()
    )
