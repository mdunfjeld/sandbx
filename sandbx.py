#!/usr/bin/env python3
"""Run an AI coding agent inside a disposable libkrun microVM, launched through podman.
With --runtime crun it is a plain podman container instead.

Each agent gets exactly one config mount and one project mount. Isolation is
structural rather than policy-based: a claude container has no copilot
credentials because agents/copilot/ is simply absent from its mount table.

    sandbx claude ~/project
    sandbx build --all
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Sequence

CONTAINER_USER = "agent"
CONTAINER_UID = 1000
CONTAINER_HOME = "/home/agent"
BASE_IMAGE = "sandbx-base"
# Where every agent's project lands inside the container. Rendered by
# workdir_for(); see there for the placeholders and why both are needed.
WORKDIR_TEMPLATE = "/workspace/{name}"
# Run instead of the agent under --shell. Same as the base image's CMD, but
# spelled out so the argv stays pure and testable.
SHELL_COMMAND = ("/bin/bash", "-l")
# Prefixed to whatever runs in a krun VM. The VM itself starts as root (see the
# --user comment in build_podman_args); this drops the agent, or the --shell
# shell, to `agent` inside the guest before it starts. crun needs no prefix.
AS_AGENT = ("setpriv", f"--reuid={CONTAINER_UID}", f"--regid={CONTAINER_UID}",
            "--init-groups", "--")
IMAGES_DIR = Path(__file__).resolve().parent / "images"

# By default a sandbox is a libkrun microVM: crun sets the container up as
# usual, then hands the process to libkrun, which boots the workload under its
# own kernel. A kernel exploit lands in the guest rather than on the host. Needs
# crun-krun and /dev/kvm.
VM_RUNTIME = "krun"
# podman's usual runtime: an ordinary namespaced container on the host kernel.
# Weaker isolation, but no KVM needed. Same mounts and network as krun, but it
# starts as `agent` directly, and the VM-sizing options below do not apply.
CONTAINER_RUNTIME = "crun"
RUNTIMES = (VM_RUNTIME, CONTAINER_RUNTIME)
# libkrun's default is 1 GiB, too little for npm or gradle. Guest memory is only
# committed as it is used, so several sandboxes of this size still fit.
VM_RAM_MIB = 4096
# crun quietly swaps a krun.ram_mib at or below this for libkrun's 1 GiB
# default, so a smaller --ram would grow the VM rather than shrink it. Refused.
VM_MIN_RAM_MIB = 128
# libkrun's hard limit. With no krun.cpus, crun gives the VM every CPU sandbx
# may run on, capped at this.
VM_MAX_CPUS = 16

# Networking is pasta with a fixed set of forwards, instead of --network=host,
# which handed over every localhost service and abstract unix socket.
#
# Outbound (-T), crun only: 127.0.0.1:30000-30009 inside reaches the same
# ports on the host. pasta listens inside each sandbox's own namespace, so any
# number of sandboxes share this range -- and the host's adb server and Burp
# with it.
TO_HOST_PORTS = (30000, 30009)
# adb defaults to 5037, outside the range. The host server must use this too.
ADB_SERVER_PORT = 30000
# A krun VM has no outbound range. It gets a virtio-net NIC served by passt
# (krun.use_passt) instead of libkrun's default TSI, which proxies every guest
# socket and holds its lock through a blocking recv(): adb shell, reading on
# one thread and writing keys on another, hung on the first key. crun starts
# that passt in the container's namespace as `passt -t all -u all --no-map-gw`,
# fixed: -t all takes every free port there, so pasta's -T listeners fail with
# "Address already in use", and --no-map-gw leaves the guest no way to reach
# them anyway. Inbound still works -- pasta's -t lands on the container's
# loopback, where passt's listener hands it to the guest -- but it arrives at
# the guest's eth0 address, so a server in the VM must listen on 0.0.0.0.
# Inbound (-t): each sandbox takes the first free block of HOST_BLOCKS, and a
# server the agent starts on one of its ports appears on the same port of the
# host's 127.0.0.1. Those are host ports, so a fixed range would let only one
# sandbox start. The numbers match on both sides on purpose: $SANDBX_HOST_PORTS
# is then right wherever you read it. The two directions must never overlap,
# or the forwards loop into each other.
BLOCK_SIZE = 10
HOST_BLOCKS = range(30010, 30100, BLOCK_SIZE)


class SandboxError(Exception):
    """Anything the user should see as a clean error rather than a traceback."""


@dataclass(frozen=True)
class Agent:
    """One agent's isolation profile. This registry is the security boundary,
    so it stays plain data -- no code path special-cases an agent by name."""

    image: str
    state_dir: str          # under <state root>/, e.g. "claude"
    config_path: str        # where it is mounted inside the container
    env: Mapping[str, str] = field(default_factory=dict)
    command: Sequence[str] = ()
    # Where the project lands inside the container, as a template rendered by
    # workdir_for(). Every agent takes the default: one rule, no per-agent
    # variation to reason about. None is the escape hatch -- it mirrors the host
    # path -- kept for an agent that someday turns out to need it, but nothing
    # uses it today.
    workdir: str | None = WORKDIR_TEMPLATE


# Each agent gets exactly one state mount; its env vars point the CLI's config
# and credentials into it. Every env value must live under config_path, or that
# data is written outside the mount and lost when the container exits.
AGENTS: dict[str, Agent] = {
    "claude": Agent(
        image="sandbx-claude",
        state_dir="claude",
        config_path=f"{CONTAINER_HOME}/.claude",
        env={"CLAUDE_CONFIG_DIR": f"{CONTAINER_HOME}/.claude"},
        command=("claude",),
    ),
    "codex": Agent(
        image="sandbx-codex",
        state_dir="codex",
        config_path=f"{CONTAINER_HOME}/.codex",
        env={"CODEX_HOME": f"{CONTAINER_HOME}/.codex"},
        command=("codex",),
    ),
    "copilot": Agent(
        image="sandbx-copilot",
        state_dir="copilot",
        config_path=f"{CONTAINER_HOME}/.copilot",
        env={"COPILOT_HOME": f"{CONTAINER_HOME}/.copilot"},
        command=("copilot",),
    ),
    # opencode has no single config-dir override: it splits config, credentials
    # (auth.json under data), sessions, and caches across the four XDG base
    # dirs. Point all four into the one mount. Side effect: any other tool in
    # this container that honours XDG also writes into opencode's state.
    "opencode": Agent(
        image="sandbx-opencode",
        state_dir="opencode",
        config_path=f"{CONTAINER_HOME}/.opencode",
        env={
            "XDG_CONFIG_HOME": f"{CONTAINER_HOME}/.opencode/config",
            "XDG_DATA_HOME": f"{CONTAINER_HOME}/.opencode/data",
            "XDG_STATE_HOME": f"{CONTAINER_HOME}/.opencode/state",
            "XDG_CACHE_HOME": f"{CONTAINER_HOME}/.opencode/cache",
        },
        command=("opencode",),
    ),
    # vibe honours VIBE_HOME for config, sessions and logs, but a few paths
    # (ACP logs among them) hardcode ~/.vibe. Mounting at exactly that path
    # makes the two agree. There is no Secret Service in the container, so the
    # API key falls back from the keyring to $VIBE_HOME/.env and persists too.
    "vibe": Agent(
        image="sandbx-vibe",
        state_dir="vibe",
        config_path=f"{CONTAINER_HOME}/.vibe",
        env={"VIBE_HOME": f"{CONTAINER_HOME}/.vibe"},
        command=("vibe",),
    ),
}


def home_dir(env: Mapping[str, str] | None = None) -> Path:
    """The user's home, resolved. A missing HOME is a clean error rather than a
    KeyError traceback -- sandbx gets run from cron and systemd units too.

    Note XDG_DATA_HOME lets state_root() skip this, but cmd_run still needs it
    for the "refusing to mount your entire home directory" guard, so in practice
    HOME is required either way.
    """
    env = os.environ if env is None else env
    home = env.get("HOME")
    if not home:
        raise SandboxError(
            "HOME is not set; sandbx needs it to locate agent state and to "
            "refuse mounting your home directory"
        )
    return Path(home).resolve()


def state_root(env: Mapping[str, str] | None = None) -> Path:
    """Host directory holding every agent's persistent state, one subdir each.

    Resolved, because validate_project() compares it against a project path that
    resolve_project() has already resolved. Leave one side unresolved and the
    comparison misses whenever a component is a symlink (~/.local/share on its
    own disk, /home -> /var/home on ostree systems), silently turning the
    "never mount the state directory" guard into a no-op.

    .resolve() is safe on a path that does not exist yet: it resolves the
    components that do and leaves the rest alone.
    """
    env = os.environ if env is None else env
    xdg = env.get("XDG_DATA_HOME")
    base = Path(xdg).resolve() if xdg else home_dir(env) / ".local" / "share"
    return (base / "sandbx" / "agents").resolve()


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------

_PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
PROFILES_VERSION = 2
DEFAULT_PROFILE = "default"


def profiles_path(env: Mapping[str, str] | None = None) -> Path:
    """The profiles.json file, sibling to the agents directory."""
    return state_root(env).parent / "profiles.json"


def _default_entry() -> dict:
    return {"profiles": [DEFAULT_PROFILE], "active": DEFAULT_PROFILE}


def _validate_entry(path: Path, agent: str, entry: object) -> None:
    where = f"malformed {path}: agents[{agent!r}]"
    if not isinstance(entry, dict):
        raise SandboxError(f"{where} must be an object")
    profiles = entry.get("profiles")
    if not isinstance(profiles, list) or not profiles:
        raise SandboxError(f"{where}: 'profiles' must be a non-empty list")
    for name in profiles:
        if not isinstance(name, str) or not _PROFILE_NAME.match(name):
            raise SandboxError(f"{where}: invalid profile name {name!r}")
    if len(set(profiles)) != len(profiles):
        raise SandboxError(f"{where}: duplicate profile names")
    if entry.get("active") not in profiles:
        raise SandboxError(
            f"{where}: active profile {entry.get('active')!r} "
            "is not in its profiles list"
        )


def _profiles_on_disk(agent_dir: Path) -> list[str]:
    """The profile state dirs under *agent_dir*, sorted. Names a profile could
    not have are skipped; they would fail validation once saved."""
    if not agent_dir.is_dir():
        return []
    return sorted(p.name for p in agent_dir.iterdir()
                  if p.is_dir() and _PROFILE_NAME.match(p.name))


def _migrate_v1(data: dict, path: Path,
                env: Mapping[str, str] | None) -> dict:
    """Split the old global profile list into one list per agent.

    Each agent keeps only the v1 profiles that mean something to it: those
    with a state dir on disk, plus "default" and its own active profile. That
    way a profile created for one agent does not show up under all the others.
    v1 let -p name any profile, listed or not, so the agent's other state dirs
    are added too; dropping them would hide the credentials they hold.
    """
    profiles = data.get("profiles")
    if not isinstance(profiles, list) or not profiles:
        raise SandboxError(f"malformed {path}: 'profiles' must be a non-empty list")
    for name in profiles:
        if not isinstance(name, str) or not _PROFILE_NAME.match(name):
            raise SandboxError(f"malformed {path}: invalid profile name {name!r}")
    if data.get("active") not in profiles:
        raise SandboxError(
            f"malformed {path}: active profile {data.get('active')!r} "
            "is not in the profiles list"
        )
    overrides = data.get("agent_active", {})
    if not isinstance(overrides, dict):
        raise SandboxError(f"malformed {path}: 'agent_active' must be an object")
    for agent, profile in overrides.items():
        if profile not in profiles:
            raise SandboxError(
                f"malformed {path}: agent_active[{agent!r}] = {profile!r} "
                "is not in the profiles list"
            )
    root = state_root(env)
    agents = {}
    for name, agent in AGENTS.items():
        active = overrides.get(name, data["active"])
        on_disk = _profiles_on_disk(root / agent.state_dir)
        keep = [p for p in dict.fromkeys(profiles)
                if p in (DEFAULT_PROFILE, active) or p in on_disk]
        keep += [p for p in on_disk if p not in keep]
        agents[name] = {"profiles": keep, "active": active}
    return {"version": PROFILES_VERSION, "agents": agents}


def load_profiles(env: Mapping[str, str] | None = None) -> dict:
    """Read profiles.json: {"version": 2, "agents": {agent: {profiles, active}}}.

    An agent absent from the file has just the implicit default profile; use
    agent_entry() rather than indexing "agents" directly. A file in the old
    global format is migrated and rewritten, the original kept alongside.
    """
    path = profiles_path(env)
    if not path.exists():
        # No file is what v1 read as the implicit default, and runs back then
        # could still have left profiles on disk. Migrate it, in memory only.
        return _migrate_v1(_default_entry(), path, env)
    try:
        raw = path.read_text()
        data = json.loads(raw)
    except (json.JSONDecodeError, OSError) as exc:
        raise SandboxError(f"cannot read {path}: {exc}") from None
    if not isinstance(data, dict):
        raise SandboxError(f"malformed {path}: expected a JSON object")
    if "agents" not in data and "profiles" in data:
        data = _migrate_v1(data, path, env)
        backup = path.with_name("profiles.v1.json")
        backup.write_text(raw)
        save_profiles(data, env)
        print(f"sandbx: migrated {path} to per-agent profiles; "
              f"the old file is kept as {backup}", file=sys.stderr)
    if (data.get("version") != PROFILES_VERSION
            or not isinstance(data.get("agents"), dict)):
        raise SandboxError(
            f"malformed {path}: expected version {PROFILES_VERSION} "
            "with an 'agents' object"
        )
    for agent, entry in data["agents"].items():
        _validate_entry(path, agent, entry)
    return data


def save_profiles(data: dict, env: Mapping[str, str] | None = None) -> None:
    """Write profiles.json atomically.

    The temp file is unique to this call. With a fixed name, every sandbx
    saving at once shares it, and the first to rename it into place leaves
    the others nothing to rename.
    """
    path = profiles_path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".profiles.",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(data, indent=2) + "\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def agent_entry(data: dict, agent: str) -> dict:
    """This agent's {"profiles", "active"} in *data*, added if absent."""
    return data["agents"].setdefault(agent, _default_entry())


def validate_profile_name(name: str) -> None:
    if not _PROFILE_NAME.match(name):
        raise SandboxError(
            f"invalid profile name {name!r}: must start with a letter or digit, "
            "contain only letters, digits, '.', '_', '-', and be 1-64 characters"
        )


def active_profile(agent: str, env: Mapping[str, str] | None = None) -> str:
    """The profile *agent* uses when --profile is not given."""
    return agent_entry(load_profiles(env), agent)["active"]


def resolve_profile(agent: str, requested: str | None,
                    env: Mapping[str, str] | None = None) -> str:
    """The profile to run *agent* with: *requested*, which must be one of the
    agent's profiles, or its active profile when None. Refusing an unknown
    name keeps a typo from silently logging in to a fresh, empty profile."""
    entry = agent_entry(load_profiles(env), agent)
    if requested is None:
        return entry["active"]
    validate_profile_name(requested)
    if requested not in entry["profiles"]:
        raise SandboxError(
            f"{agent} has no profile {requested!r}; create it with "
            f"`sandbx profile create {agent} {requested}`"
        )
    return requested


def assert_path_within(target: Path, expected_parent: Path,
                       root: Path) -> None:
    """Raise if *target* is not a direct child of *expected_parent* or escapes *root*."""
    if target.parent != expected_parent or not target.resolve().is_relative_to(root):
        raise SandboxError(f"refusing to delete {target}: unexpected path")


def validate_project(project: Path, home: Path, agents_root: Path) -> None:
    """Reject project paths that would defeat the point of the sandbox.

    Pure (no filesystem access) so it can be tested against synthetic paths.
    """
    if not project.is_absolute():
        raise SandboxError(f"project path must be absolute: {project}")
    if project == Path(project.anchor):
        raise SandboxError("refusing to mount the filesystem root")
    if project == home:
        raise SandboxError(
            f"refusing to mount your entire home directory ({home}); "
            "point at a specific project instead"
        )
    if agents_root == project or agents_root.is_relative_to(project):
        raise SandboxError(
            f"refusing to mount {project}: it contains the agent state directory "
            f"({agents_root}), which would expose every agent's credentials"
        )
    if project.is_relative_to(agents_root):
        raise SandboxError(
            f"refusing to mount {project}: it lives inside the agent state directory"
        )


_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def workdir_for(agent: Agent, project: Path) -> str:
    """Where this agent's project mount lands inside the container.

    A None workdir mirrors the host cwd basename.
    """
    if agent.workdir is None:
        return str(project)
    name = _UNSAFE_NAME.sub("-", project.name).strip("-.") or "project"
    try:
        return agent.workdir.format(name=name)
    except (KeyError, IndexError) as exc:
        raise SandboxError(
            f"bad workdir template {agent.workdir!r}: unknown placeholder {exc}"
        ) from None


def validate_agent(name: str, agent: Agent) -> None:
    """Reject a registry entry that is malformed, before it becomes a container.

    Everything sandbx does is driven by this data, so a typo here does not fail
    loudly on its own -- it produces a container that is subtly wrong. Checking
    up front keeps "add an agent" from being a step that needs care: get a field
    wrong and you get a sentence, not a broken sandbox.
    """
    if not agent.image:
        raise SandboxError(f"{name}: registry entry has no image")
    if not agent.command:
        raise SandboxError(f"{name}: registry entry has no command to run")
    if not agent.state_dir or "/" in agent.state_dir or agent.state_dir.startswith("."):
        raise SandboxError(
            f"{name}: state_dir must be a single plain directory name, "
            f"got {agent.state_dir!r}"
        )
    if not agent.config_path.startswith("/"):
        raise SandboxError(
            f"{name}: config_path must be absolute, got {agent.config_path!r}"
        )
    for key, value in agent.env.items():
        if value != agent.config_path and not value.startswith(agent.config_path + "/"):
            raise SandboxError(
                f"{name}: {key}={value} is outside config_path "
                f"({agent.config_path}); that data would not persist"
            )
    for other_name, other in AGENTS.items():
        if other_name == name:
            continue
        if other.state_dir == agent.state_dir:
            raise SandboxError(
                f"{name} and {other_name} share state_dir {agent.state_dir!r}; "
                "they would see each other's credentials"
            )
        if other.config_path == agent.config_path:
            raise SandboxError(
                f"{name} and {other_name} share config_path {agent.config_path!r}"
            )


def validate_registry() -> None:
    """Validate every agent, not just the one being acted on.

    Called from main() before dispatch, so a malformed entry fails the same way
    whichever command you ran. The alternative -- each command validating the
    agent it touches -- is how cmd_reset came to rmtree a path it had never
    checked: correctness depended on remembering the call in each new code path.
    """
    for name, agent in AGENTS.items():
        validate_agent(name, agent)


def mount_spec(src: str, dest: str, suffix: str) -> str:
    """Render one `-v` argument, refusing paths podman cannot parse unambiguously.

    podman splits `-v` on ":" and offers no escape for a literal one, so a path
    containing a colon does not fail cleanly -- it reshapes the mount table into
    something other than what was asked for. For a tool whose only job is keeping
    that table predictable, mangling is the wrong failure mode. Reject instead.
    """
    for label, value in (("source", src), ("destination", dest)):
        if ":" in value:
            raise SandboxError(
                f"cannot mount a path containing ':' ({label}: {value})\n"
                "podman's -v syntax has no way to escape it; rename the directory"
            )
    return f"{src}:{dest}{suffix}"


def port_span(ports: tuple[int, int]) -> str:
    """Render a (first, last) pair the way pasta and humans both read it."""
    return f"{ports[0]}-{ports[1]}"


def block_ports(block: int) -> tuple[int, int]:
    """First and last port of one inbound block, the same inside and on the host."""
    return block, block + BLOCK_SIZE - 1


def _port_free(port: int) -> bool:
    """Whether 127.0.0.1:port could be listened on right now.

    SO_REUSEADDR so that connections lingering in TIME_WAIT -- left behind by a
    sandbox that just exited -- don't count as taken; only a live listener does.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def pick_host_block(port_free: Callable[[int], bool] = _port_free) -> int:
    """The first block of HOST_BLOCKS whose ports are all free on the host.

    Check now, bind later: a port taken in between makes podman fail with
    pasta's bind error, and a rerun picks another block. Closing that gap would
    mean holding the sockets across exec, which pasta cannot take over anyway.
    """
    for block in HOST_BLOCKS:
        first, last = block_ports(block)
        if all(port_free(port) for port in range(first, last + 1)):
            return block
    span = (HOST_BLOCKS[0], block_ports(HOST_BLOCKS[-1])[1])
    raise SandboxError(
        f"no free inbound port block in {port_span(span)}: {len(HOST_BLOCKS)} "
        "sandboxes are already running, or something else holds those ports"
    )


def have_kvm() -> bool:
    """Whether this user can open /dev/kvm, which every microVM needs."""
    return os.access("/dev/kvm", os.R_OK | os.W_OK)


_RAM_SIZE = re.compile(r"(\d+)([MG]?)", re.IGNORECASE)


def parse_ram(value: str) -> int:
    """--ram as MiB: a bare number is MiB, or give an M or G suffix. The range
    is build_podman_args()'s to check, so every caller gets the same rule."""
    match = _RAM_SIZE.fullmatch(value)
    if not match:
        raise argparse.ArgumentTypeError(
            f"invalid size {value!r}; give MiB (8192) or a G or M suffix (8G)"
        )
    amount = int(match[1])
    if amount == 0:
        raise argparse.ArgumentTypeError("RAM size must be greater than zero")
    return amount * 1024 if match[2].upper() == "G" else amount


def build_podman_args(
    name: str,
    project: Path,
    *,
    agents_root: Path,
    profile: str = "default",
    relabel: bool = True,
    interactive: bool = True,
    extra: Sequence[str] = (),
    shell: bool = False,
    host_block: int = HOST_BLOCKS[0],
    runtime: str = VM_RUNTIME,
    ram_mib: int | None = None,
    cpus: int | None = None,
) -> list[str]:
    """Construct the full podman argv. Pure, so tests can assert the isolation
    invariants directly instead of eyeballing a command line.

    shell=True swaps only the trailing command for SHELL_COMMAND; the sandbox
    itself is identical, so the agent can be started by hand inside it.

    host_block is this sandbox's share of the inbound host ports; cmd_run picks
    it with pick_host_block(), since that part needs to look at the host.

    runtime is one of RUNTIMES. Mounts, inbound ports and env are the same
    under each; what differs is the runtime flag, the VM annotations,
    SANDBX_RUNTIME, how the workload becomes `agent` (see the --user comment),
    and the outbound range with its ANDROID_ADB_SERVER_PORT, which only crun
    gets (see TO_HOST_PORTS).

    ram_mib and cpus size the VM, so they are refused unless runtime is krun.
    ram_mib=None means VM_RAM_MIB; cpus=None leaves the count to crun.
    """
    try:
        agent = AGENTS[name]
    except KeyError:
        raise SandboxError(
            f"unknown agent {name!r} (known: {', '.join(sorted(AGENTS))})"
        ) from None

    validate_agent(name, agent)
    if shell and extra:
        raise SandboxError("--shell cannot be combined with -- ARGS")
    if host_block not in HOST_BLOCKS:
        raise SandboxError(
            f"host port block {host_block} is not one of the reserved blocks "
            f"({', '.join(map(str, HOST_BLOCKS))})"
        )
    if runtime not in RUNTIMES:
        raise SandboxError(
            f"unknown runtime {runtime!r} (known: {', '.join(RUNTIMES)})"
        )
    if runtime == VM_RUNTIME:
        if ram_mib is None:
            ram_mib = VM_RAM_MIB
        if ram_mib <= VM_MIN_RAM_MIB:
            raise SandboxError(
                f"--ram must be more than {VM_MIN_RAM_MIB} MiB; crun replaces "
                f"anything smaller with libkrun's 1 GiB default"
            )
        if cpus is not None and not 1 <= cpus <= VM_MAX_CPUS:
            raise SandboxError(
                f"--cpus must be 1-{VM_MAX_CPUS}, libkrun's limit; got {cpus}"
            )
    elif ram_mib is not None or cpus is not None:
        raise SandboxError(
            f"--ram and --cpus size a microVM; they need --runtime {VM_RUNTIME}"
        )
    inbound = port_span(block_ports(host_block))

    validate_profile_name(profile)
    config_src = agents_root / agent.state_dir / profile
    # ":z" (shared), not ":Z" (private). Containers here are ephemeral and get a
    # fresh MCS category per run, so ":Z" would relabel the whole project tree on
    # every single launch. Side effect: the project dir becomes container_file_t
    # on the host, which --no-relabel opts out of.
    suffix = ":z" if relabel else ""

    # Destination of the project mount, and the container cwd. Same rule for
    # every agent; workdir=None would mirror the host path instead.
    dest = workdir_for(agent, project)
    if not dest.startswith("/"):
        raise SandboxError(f"{name}: workdir must be absolute, got {dest!r}")
    if dest == agent.config_path or Path(agent.config_path).is_relative_to(dest):
        raise SandboxError(
            f"{name}: workdir {dest} would shadow the config mount "
            f"({agent.config_path})"
        )

    # krun runs the VMM as the container user, and the VMM is what opens every
    # file the guest touches. As `agent` it cannot open root-owned files, so
    # sudo and every root write in the guest fail with EACCES. So a VM starts
    # as root in the user namespace -- the identity sudo had before krun, not
    # host root -- and AS_AGENT drops the workload back to `agent`. virtio-fs
    # creates files with the requesting guest uid, so through keep-id they
    # still come out owned by you. crun has no VMM in the way and starts as
    # `agent` directly.
    vm = runtime == VM_RUNTIME
    user = "0:0" if vm else f"{CONTAINER_UID}:{CONTAINER_UID}"

    # Podman splits pasta options on commas, so no single spec may hold one --
    # ranges only, never lists or `auto` combined with a range. No ":to" part,
    # so each host port lands on the same port inside.
    pasta = f"-t,127.0.0.1/{inbound}"
    if not vm:
        # A VM's passt already holds these ports; see TO_HOST_PORTS.
        pasta = f"-T,{port_span(TO_HOST_PORTS)},{pasta}"

    args = ["podman", "run", "--rm", f"--runtime={runtime}"]
    if interactive:
        args.append("-it")
    args += [
        # Pinned rather than bare keep-id: bare would map whatever the host uid
        # happens to be, which only lines up with `agent` when that uid is 1000.
        f"--userns=keep-id:uid={CONTAINER_UID},gid={CONTAINER_UID}",
        "--user", user,
        f"--network=pasta:{pasta}",
    ]
    if vm:
        args += ["--annotation", f"krun.ram_mib={ram_mib}"]
        if cpus is not None:
            args += ["--annotation", f"krun.cpus={cpus}"]
        # virtio-net through passt rather than TSI; see TO_HOST_PORTS.
        args += ["--annotation", "krun.use_passt=1"]
    if not relabel:
        args += ["--security-opt", "label=disable"]
    args += [
        "-v", mount_spec(str(config_src), agent.config_path, suffix),
        "-v", mount_spec(str(project), dest, suffix),
        "-w", dest,
        # Podman derives HOME from --user, which would make it /root in a VM.
        "-e", f"HOME={CONTAINER_HOME}",
        "-e", f"SANDBX_AGENT={name}",
        "-e", f"SANDBX_PROFILE={profile}",
        # So the agent, and whoever is in the shell, can tell a VM from a container.
        "-e", f"SANDBX_RUNTIME={runtime}",
        # The ports to serve on for the host to see them, so the agent can say so.
        "-e", f"SANDBX_HOST_PORTS={inbound}",
    ]
    if not vm:
        # Only crun can reach the host's adb server; see TO_HOST_PORTS.
        args += ["-e", f"ANDROID_ADB_SERVER_PORT={ADB_SERVER_PORT}"]
    for key, value in agent.env.items():
        args += ["-e", f"{key}={value}"]
    args.append(agent.image)
    if vm:
        args += list(AS_AGENT)
    if shell:
        args += list(SHELL_COMMAND)
    else:
        args += list(agent.command)
        args += list(extra)
    return args


def resolve_project(raw: str) -> Path:
    """Resolve to a real absolute path, following symlinks, so a link cannot
    smuggle a different subtree into the container."""
    project = Path(raw).expanduser().resolve()
    if not project.exists():
        raise SandboxError(f"no such directory: {project}")
    if not project.is_dir():
        raise SandboxError(f"not a directory: {project}")
    return project


def use_color(stream=None, env: Mapping[str, str] | None = None) -> bool:
    """Whether to color what goes to *stream* (default stderr): only when it is
    a terminal, and never with NO_COLOR set (no-color.org) or TERM=dumb."""
    stream = sys.stderr if stream is None else stream
    env = os.environ if env is None else env
    if env.get("NO_COLOR") or env.get("TERM") == "dumb":
        return False
    try:
        return stream.isatty()
    except (AttributeError, ValueError):  # no isatty, or already closed
        return False


def _ansi(text: str, code: str, color: bool) -> str:
    """*text* in SGR *code* (e.g. "1;36"), or untouched when not coloring."""
    return f"\033[{code}m{text}\033[0m" if color else text


# The banner's runtime row: a color and a few words on what isolation you got.
# Yellow for crun, because a plain container shares the host kernel.
RUNTIME_BANNER = {
    VM_RUNTIME: ("1;32", "microVM, own kernel"),
    CONTAINER_RUNTIME: ("1;33", "container, host kernel"),
}
_BANNER_TITLE = " sandbx "
_BANNER_MIN_WIDTH = 46


def sandbox_banner(name: str, profile: str, runtime: str, host_block: int, *,
                   shell: bool = False, color: bool = False) -> str:
    """The box every run prints before the sandbox starts; shell=True adds a
    hint below it on how to start the agent. Pure, so tests can check it; every
    cell is padded as plain text and only then colored, so the right border
    lines up whether or not color is on.
    """
    try:
        runtime_code, runtime_desc = RUNTIME_BANNER[runtime]
    except KeyError:
        raise SandboxError(
            f"unknown runtime {runtime!r} (known: {', '.join(RUNTIMES)})"
        ) from None
    # (label, [(text, SGR code), ...]) per row; cells are two spaces apart.
    rows = [
        ("agent", [(name, "1")]),
        ("profile", [(profile, "1")]),
        ("runtime", [(runtime, runtime_code), (runtime_desc, "2")]),
        ("ports", [(port_span(block_ports(host_block)), "1;36"),
                   ("$SANDBX_HOST_PORTS", "2")]),
    ]
    label_w = max(len(label) for label, _ in rows) + 4

    def plain(cells):
        return "  ".join(text for text, _ in cells)

    width = max(_BANNER_MIN_WIDTH,
                *(2 + label_w + len(plain(cells)) + 2 for _, cells in rows))

    def border(text: str) -> str:
        return _ansi(text, "36", color)

    lines = [border("╭─") + _ansi(_BANNER_TITLE, "1;35", color)
             + border("─" * (width - 1 - len(_BANNER_TITLE)) + "╮")]
    for label, cells in rows:
        pad = width - 2 - label_w - len(plain(cells))
        value = "  ".join(_ansi(text, code, color) for text, code in cells)
        lines.append(border("│") + "  " + _ansi(label.ljust(label_w), "2", color)
                     + value + " " * pad + border("│"))
    lines.append(border("╰" + "─" * width + "╯"))
    if shell:
        command = shlex.join(AGENTS[name].command)
        lines.append("  " + _ansi(f"{name} not started; run ", "2", color)
                     + _ansi(f"`{command}`", "1", color)
                     + _ansi(" to launch it", "2", color))
    return "\n".join(lines)


def split_passthrough(argv: Sequence[str]) -> tuple[list[str], list[str]]:
    """Split on the first `--`; everything after it goes to the agent."""
    argv = list(argv)
    if "--" in argv:
        i = argv.index("--")
        return argv[:i], argv[i + 1:]
    return argv, []


def cmd_run(name: str, argv: Sequence[str]) -> int:
    ours, passthrough = split_passthrough(argv)
    parser = argparse.ArgumentParser(
        prog=f"sandbx {name}",
        description=f"Run {name} in a disposable sandbox.",
    )
    parser.add_argument("path", nargs="?", default=".",
                        help="project directory to expose (default: cwd)")
    parser.add_argument("--no-relabel", action="store_true",
                        help="skip SELinux :z relabeling; disable label confinement instead")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the podman command without running it")
    parser.add_argument("-p", "--profile", default=None,
                        help=f"one of {name}'s credential profiles "
                             f"(default: its active profile)")
    parser.add_argument("-s", "--shell", action="store_true",
                        help=f"open a bash shell in the sandbox instead of starting "
                             f"{name}; run `{shlex.join(AGENTS[name].command)}` "
                             f"from it when ready")
    parser.add_argument("--runtime", choices=RUNTIMES, default=VM_RUNTIME,
                        help=f"{VM_RUNTIME}: a microVM with its own kernel (default); "
                             f"{CONTAINER_RUNTIME}: a plain container on the host "
                             f"kernel, no KVM needed")
    # Every option in this group is refused under any other runtime, so each
    # must default to None: that is how an explicit one is told apart.
    vm = parser.add_argument_group(f"microVM options (--runtime {VM_RUNTIME} only)")
    vm_only = [
        vm.add_argument("--ram", type=parse_ram, metavar="SIZE",
                        help=f"VM memory: MiB, or with a G or M suffix "
                             f"(default: {VM_RAM_MIB // 1024}G)"),
        vm.add_argument("--cpus", type=int, metavar="N",
                        help=f"VM vCPUs, 1-{VM_MAX_CPUS} (default: every CPU "
                             f"sandbx may run on, up to {VM_MAX_CPUS})"),
    ]
    opts = parser.parse_args(ours)
    if opts.shell and passthrough:
        parser.error("--shell cannot be combined with -- ARGS")
    if opts.runtime != VM_RUNTIME:
        for action in vm_only:
            if getattr(opts, action.dest) is not None:
                parser.error(f"{action.option_strings[-1]} needs --runtime "
                             f"{VM_RUNTIME}; a {opts.runtime} sandbox is not a VM")

    profile = resolve_profile(name, opts.profile)

    project = resolve_project(opts.path)
    agents_root = state_root()
    validate_project(project, home_dir(), agents_root)

    # Picked for --dry-run too, so the printed command is the one that would run.
    host_block = pick_host_block()
    args = build_podman_args(
        name, project,
        agents_root=agents_root,
        profile=profile,
        relabel=not opts.no_relabel,
        interactive=sys.stdin.isatty(),
        extra=passthrough,
        shell=opts.shell,
        host_block=host_block,
        runtime=opts.runtime,
        ram_mib=opts.ram,
        cpus=opts.cpus,
    )

    if opts.dry_run:
        print(shlex.join(args))
        return 0

    if shutil.which("podman") is None:
        raise SandboxError("podman not found on PATH")
    if opts.runtime == VM_RUNTIME and not have_kvm():
        raise SandboxError(
            f"cannot open /dev/kvm; a {VM_RUNTIME} sandbox is a microVM and needs "
            "KVM. Check that virtualization is enabled and /dev/kvm is read-write "
            f"for you, or use --runtime {CONTAINER_RUNTIME} for a plain container"
        )

    # Bind mounts get no copy-up, so podman would create this root-owned. Do it
    # after the checks above so a failed launch leaves no stray state behind.
    (agents_root / AGENTS[name].state_dir / profile).mkdir(
        parents=True, exist_ok=True)
    print(sandbox_banner(name, profile, opts.runtime, host_block,
                         shell=opts.shell, color=use_color()), file=sys.stderr)
    try:
        os.execvp("podman", args)  # replaces this process; keeps the TTY clean
    except OSError as exc:
        raise SandboxError(f"failed to exec podman: {exc}") from exc


def cmd_build(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="sandbx build")
    parser.add_argument("agent", nargs="?", choices=sorted(AGENTS),
                        help="agent image to build (default: --all)")
    parser.add_argument("--all", action="store_true", help="build every agent image")
    base = parser.add_mutually_exclusive_group()
    base.add_argument("--base", action="store_true",
                      help="also rebuild the shared base image (default: only if missing)")
    base.add_argument("--no-base", action="store_true",
                      help="never build the base image, even if it is missing")
    parser.add_argument("--no-cache", action="store_true",
                        help="ignore cached layers; needed to pick up new agent versions")
    parser.add_argument("--pull", action="store_true",
                        help="re-fetch ubuntu:24.04 (applies to the base image only)")
    opts = parser.parse_args(list(argv))

    if not opts.agent and not opts.all:
        opts.all = True
    targets = sorted(AGENTS) if opts.all else [opts.agent]

    # The base changes rarely and takes minutes; agent images are the ones that
    # need updating. So build it only when asked, or when it does not exist yet
    # -- without it the agent builds fail on an unknown FROM.
    if opts.base or (not opts.no_base and not image_exists(BASE_IMAGE)):
        _build_image(BASE_IMAGE, IMAGES_DIR / "Containerfile.base",
                     no_cache=opts.no_cache, pull=opts.pull)
    for name in targets:
        # --pull only concerns the registry base (ubuntu:24.04), which the agent
        # images do not reference; passing it here would be a no-op at best.
        _build_image(AGENTS[name].image, IMAGES_DIR / f"Containerfile.{name}",
                     no_cache=True)
    return 0


def build_image_cmd(tag: str, containerfile: Path, *,
                    no_cache: bool = False, pull: bool = False) -> list[str]:
    """The podman build argv. Pure, so the flags can be asserted in tests.

    --no-cache is what makes a rebuild actually reinstall the agent: the
    install step is a single RUN layer, so without it podman reuses the cached
    layer and the image keeps whatever version it was first built with.
    """
    cmd = ["podman", "build"]
    if no_cache:
        cmd.append("--no-cache")
    if pull:
        cmd.append("--pull")
    return cmd + ["-t", tag, "-f", str(containerfile), str(IMAGES_DIR)]


def _build_image(tag: str, containerfile: Path, *,
                 no_cache: bool = False, pull: bool = False) -> None:
    if not containerfile.exists():
        raise SandboxError(f"missing {containerfile}")
    cmd = build_image_cmd(tag, containerfile, no_cache=no_cache, pull=pull)
    print(f"==> {shlex.join(cmd)}", file=sys.stderr)
    result = subprocess.run(cmd)
    if result.returncode != 0:
        raise SandboxError(f"build failed for {tag}")


def image_exists(tag: str) -> bool:
    """Whether podman already has this image locally."""
    if shutil.which("podman") is None:
        return False
    probe = subprocess.run(["podman", "image", "exists", tag], capture_output=True)
    return probe.returncode == 0


def cmd_list(argv: Sequence[str]) -> int:
    argparse.ArgumentParser(prog="sandbx list").parse_args(list(argv))
    have_podman = shutil.which("podman") is not None
    data = load_profiles()
    print(f"{'AGENT':<10} {'IMAGE':<22} {'BUILT':<7} {'ACTIVE':<12} PROFILES")
    for name in sorted(AGENTS):
        agent = AGENTS[name]
        built = "yes" if image_exists(agent.image) else "no" if have_podman else "?"
        entry = agent_entry(data, name)
        print(f"{name:<10} {agent.image:<22} {built:<7} {entry['active']:<12} "
              f"{', '.join(entry['profiles'])}")
    return 0


def cmd_reset(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="sandbx reset",
        description="Delete an agent's persistent state, including its credentials.",
    )
    parser.add_argument("agent", choices=sorted(AGENTS))
    parser.add_argument("-p", "--profile", default=None,
                        help="profile to reset (default: the agent's active profile)")
    parser.add_argument("--all-profiles", action="store_true",
                        help="reset all profiles for this agent")
    parser.add_argument("-y", "--yes", action="store_true", help="skip confirmation")
    opts = parser.parse_args(list(argv))

    if opts.profile and opts.all_profiles:
        parser.error("--profile and --all-profiles are mutually exclusive")

    root = state_root()
    agent_dir = root / AGENTS[opts.agent].state_dir

    if opts.all_profiles:
        target = agent_dir
        assert_path_within(target, root, root)
        desc = f"all profiles for {opts.agent}"
        confirm_text = opts.agent
    else:
        profile = opts.profile or active_profile(opts.agent)
        validate_profile_name(profile)
        target = agent_dir / profile
        assert_path_within(target, agent_dir, root)
        desc = f"{opts.agent} profile {profile!r}"
        confirm_text = f"{opts.agent}/{profile}"

    if not target.exists():
        print(f"nothing to reset: {target} does not exist")
        return 0
    if not opts.yes:
        print(f"This deletes {target}, including {desc}'s stored credentials")
        print("and session history. This cannot be undone.")
        if input(f"Type '{confirm_text}' to confirm: ").strip() != confirm_text:
            print("aborted")
            return 1
    shutil.rmtree(target)
    print(f"removed {target}")
    return 0


# ---------------------------------------------------------------------------
# sandbx profile
# ---------------------------------------------------------------------------

def cmd_profile(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="sandbx profile",
        description="Manage each agent's credential profiles. Every agent has "
                    "its own profiles and its own active profile.",
    )
    sub = parser.add_subparsers(dest="action", required=True)
    agents = sorted(AGENTS)

    p_list = sub.add_parser("list", help="list profiles and which is active")
    p_list.add_argument("agent", nargs="?", choices=agents,
                        help="only this agent (default: every agent)")

    p_create = sub.add_parser("create", help="create a profile for an agent")
    p_create.add_argument("agent", choices=agents)
    p_create.add_argument("name", help="profile name to create")

    p_rename = sub.add_parser("rename", help="rename one of an agent's profiles")
    p_rename.add_argument("agent", choices=agents)
    p_rename.add_argument("old", help="current name")
    p_rename.add_argument("new", help="new name")

    p_delete = sub.add_parser("delete",
                              help="delete one of an agent's profiles and its state")
    p_delete.add_argument("agent", choices=agents)
    p_delete.add_argument("name", help="profile to delete")
    p_delete.add_argument("-y", "--yes", action="store_true",
                          help="skip confirmation")

    p_set = sub.add_parser("set", help="set an agent's active profile")
    p_set.add_argument("agent", choices=agents)
    p_set.add_argument("name", help="profile to make active")

    opts = parser.parse_args(list(argv))

    dispatch = {
        "list":   lambda: _profile_list(opts.agent),
        "create": lambda: _profile_create(opts.agent, opts.name),
        "rename": lambda: _profile_rename(opts.agent, opts.old, opts.new),
        "delete": lambda: _profile_delete(opts.agent, opts.name, yes=opts.yes),
        "set":    lambda: _profile_set(opts.agent, opts.name),
    }
    return dispatch[opts.action]()


def _profile_list(agent: str | None = None) -> int:
    data = load_profiles()
    for name in [agent] if agent else sorted(AGENTS):
        entry = agent_entry(data, name)
        print(f"{name}:")
        for profile in entry["profiles"]:
            marker = " (active)" if profile == entry["active"] else ""
            print(f"  {profile}{marker}")
    return 0


def _profile_create(agent: str, name: str) -> int:
    validate_profile_name(name)
    data = load_profiles()
    entry = agent_entry(data, agent)
    if name in entry["profiles"]:
        raise SandboxError(f"{agent} already has a profile {name!r}")
    state = state_root() / AGENTS[agent].state_dir / name
    entry["profiles"].append(name)
    save_profiles(data)
    print(f"created {agent} profile {name!r}")
    # Taking over state left without a profile is how it gets recovered, so
    # allow it, but say so: that state may hold someone's credentials.
    if state.exists():
        print(f"sandbx: {state} already existed; the new profile uses the "
              "state in it, stored credentials included. Run `sandbx reset "
              f"{agent} -p {name}` to start it empty.", file=sys.stderr)
    return 0


def _no_such_profile(agent: str, name: str) -> SandboxError:
    return SandboxError(
        f"{agent} has no profile {name!r}; see `sandbx profile list {agent}`"
    )


def _profile_rename(agent: str, old: str, new: str) -> int:
    validate_profile_name(old)
    validate_profile_name(new)
    data = load_profiles()
    entry = agent_entry(data, agent)
    if old not in entry["profiles"]:
        raise _no_such_profile(agent, old)
    if new in entry["profiles"]:
        raise SandboxError(f"{agent} already has a profile {new!r}")
    root = state_root()
    agent_dir = root / AGENTS[agent].state_dir
    src, dst = agent_dir / old, agent_dir / new
    assert_path_within(src, agent_dir, root)
    assert_path_within(dst, agent_dir, root)
    # A leftover directory under the new name would otherwise be adopted,
    # credentials and all, by the renamed profile.
    if dst.exists():
        raise SandboxError(f"cannot rename: {dst} already exists")
    if src.exists():
        src.rename(dst)
    entry["profiles"][entry["profiles"].index(old)] = new
    if entry["active"] == old:
        entry["active"] = new
    save_profiles(data)
    print(f"renamed {agent} profile {old!r} -> {new!r}")
    return 0


def _profile_delete(agent: str, name: str, *, yes: bool = False) -> int:
    validate_profile_name(name)
    data = load_profiles()
    entry = agent_entry(data, agent)
    if name not in entry["profiles"]:
        raise _no_such_profile(agent, name)
    # The active profile is always in the list, so this also covers the last one.
    if name == entry["active"]:
        raise SandboxError(
            f"cannot delete {agent}'s active profile {name!r}; switch to "
            f"another first with `sandbx profile set {agent} <name>`"
        )
    root = state_root()
    agent_dir = root / AGENTS[agent].state_dir
    target = agent_dir / name
    assert_path_within(target, agent_dir, root)
    if not yes:
        confirm_text = f"{agent}/{name}"
        print(f"This deletes {agent} profile {name!r}, including its stored "
              "credentials")
        print("and session history. This cannot be undone.")
        if input(f"Type '{confirm_text}' to confirm: ").strip() != confirm_text:
            print("aborted")
            return 1
    entry["profiles"].remove(name)
    save_profiles(data)
    if target.exists():
        shutil.rmtree(target)
        print(f"  removed {target}")
    print(f"deleted {agent} profile {name!r}")
    return 0


def _profile_set(agent: str, name: str) -> int:
    data = load_profiles()
    entry = agent_entry(data, agent)
    if name not in entry["profiles"]:
        raise _no_such_profile(agent, name)
    entry["active"] = name
    save_profiles(data)
    print(f"{agent} active profile: {name}")
    return 0


USAGE = f"""usage: sandbx <agent> [path] [options]
       sandbx build [agent|--all]
       sandbx list
       sandbx reset <agent> [-p PROFILE|--all-profiles]
       sandbx profile list [agent]
       sandbx profile create|delete|set <agent> <name>
       sandbx profile rename <agent> <old> <new>

agents: {', '.join(sorted(AGENTS))}
Each agent has its own profiles; run `sandbx profile list` to see them.

Run `sandbx <agent> --help` for per-agent options.
"""


def main(argv: Sequence[str]) -> int:
    argv = list(argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE, end="")
        return 0 if argv else 1

    validate_registry()

    command, rest = argv[0], argv[1:]
    if command in AGENTS:
        return cmd_run(command, rest)
    dispatch = {"build": cmd_build, "list": cmd_list, "reset": cmd_reset,
                "profile": cmd_profile}
    if command in dispatch:
        return dispatch[command](rest)

    print(f"sandbx: unknown agent or command {command!r}\n", file=sys.stderr)
    print(USAGE, end="", file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SandboxError as exc:
        print(f"sandbx: {exc}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        sys.exit(130)
