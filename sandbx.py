#!/usr/bin/env python3
"""Run an AI coding agent inside a disposable podman container.

Each agent gets exactly one config mount and one project mount. Isolation is
structural rather than policy-based: a claude container has no copilot
credentials because agents/copilot/ is simply absent from its mount table.

    sandbx claude ~/project
    sandbx build --all
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

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
IMAGES_DIR = Path(__file__).resolve().parent / "images"


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


def build_podman_args(
    name: str,
    project: Path,
    *,
    agents_root: Path,
    relabel: bool = True,
    interactive: bool = True,
    extra: Sequence[str] = (),
    shell: bool = False,
) -> list[str]:
    """Construct the full podman argv. Pure, so tests can assert the isolation
    invariants directly instead of eyeballing a command line.

    shell=True swaps only the trailing command for SHELL_COMMAND; the sandbox
    itself is identical, so the agent can be started by hand inside it.
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

    config_src = agents_root / agent.state_dir
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

    args = ["podman", "run", "--rm"]
    if interactive:
        args.append("-it")
    args += [
        # Pinned rather than bare keep-id: bare would map whatever the host uid
        # happens to be, which only lines up with `agent` when that uid is 1000.
        f"--userns=keep-id:uid={CONTAINER_UID},gid={CONTAINER_UID}",
        "--network=host",
    ]
    if not relabel:
        args += ["--security-opt", "label=disable"]
    args += [
        "-v", mount_spec(str(config_src), agent.config_path, suffix),
        "-v", mount_spec(str(project), dest, suffix),
        "-w", dest,
        "-e", f"SANDBX_AGENT={name}",
    ]
    for key, value in agent.env.items():
        args += ["-e", f"{key}={value}"]
    args.append(agent.image)
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
    parser.add_argument("-s", "--shell", action="store_true",
                        help=f"open a bash shell in the sandbox instead of starting "
                             f"{name}; run `{shlex.join(AGENTS[name].command)}` "
                             f"from it when ready")
    opts = parser.parse_args(ours)
    if opts.shell and passthrough:
        parser.error("--shell cannot be combined with -- ARGS")

    project = resolve_project(opts.path)
    agents_root = state_root()
    validate_project(project, home_dir(), agents_root)

    args = build_podman_args(
        name, project,
        agents_root=agents_root,
        relabel=not opts.no_relabel,
        interactive=sys.stdin.isatty(),
        extra=passthrough,
        shell=opts.shell,
    )

    if opts.dry_run:
        print(shlex.join(args))
        return 0

    if shutil.which("podman") is None:
        raise SandboxError("podman not found on PATH")

    # Bind mounts get no copy-up, so podman would create this root-owned. Do it
    # after the podman check so a failed launch leaves no stray state behind.
    (agents_root / AGENTS[name].state_dir).mkdir(parents=True, exist_ok=True)
    if opts.shell:
        print(f"sandbx: {name} not started; run "
              f"`{shlex.join(AGENTS[name].command)}` to launch it", file=sys.stderr)
    os.execvp("podman", args)  # replaces this process; keeps the TTY clean


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
                     no_cache=opts.no_cache)
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
    agents_root = state_root()
    have_podman = shutil.which("podman") is not None
    print(f"{'AGENT':<10} {'IMAGE':<22} {'BUILT':<7} STATE")
    for name in sorted(AGENTS):
        agent = AGENTS[name]
        built = "yes" if image_exists(agent.image) else "no" if have_podman else "?"
        state = agents_root / agent.state_dir
        marker = str(state) if state.exists() else f"{state} (empty)"
        print(f"{name:<10} {agent.image:<22} {built:<7} {marker}")
    return 0


def cmd_reset(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="sandbx reset",
        description="Delete an agent's persistent state, including its credentials.",
    )
    parser.add_argument("agent", choices=sorted(AGENTS))
    parser.add_argument("-y", "--yes", action="store_true", help="skip confirmation")
    opts = parser.parse_args(list(argv))

    root = state_root()
    target = root / AGENTS[opts.agent].state_dir
    # validate_registry() has already vetted state_dir, but this one deletes
    # things: confirm independently that the path really is inside the state
    # root before handing it to rmtree.
    if target.parent != root or not target.resolve().is_relative_to(root):
        raise SandboxError(
            f"refusing to delete {target}: not a direct child of {root}"
        )
    if not target.exists():
        print(f"nothing to reset: {target} does not exist")
        return 0
    if not opts.yes:
        print(f"This deletes {target}, including {opts.agent}'s stored credentials")
        print("and session history. This cannot be undone.")
        if input("Type the agent name to confirm: ").strip() != opts.agent:
            print("aborted")
            return 1
    shutil.rmtree(target)
    print(f"removed {target}")
    return 0


USAGE = f"""usage: sandbx <agent> [path] [options]
       sandbx build [agent|--all]
       sandbx list
       sandbx reset <agent>

agents: {', '.join(sorted(AGENTS))}

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
    dispatch = {"build": cmd_build, "list": cmd_list, "reset": cmd_reset}
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
