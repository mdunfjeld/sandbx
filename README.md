# sandbx

Run an AI coding agent in a disposable libkrun microVM, launched through podman,
where the only access to the host filesystem is the selected project directory
and that agent's own credentials — nothing else.

```sh
./install.sh                   # symlink sandbx into ~/.local/bin
sandbx build                   # build all images (once)
sandbx claude ~/project        # launch
```

The VM is deleted on exit; the agent's config and session history persist.
Several sandboxes can run at once.

## Requirements

- podman with the `krun` runtime: `sudo dnf install crun-krun` on Fedora.
- `/dev/kvm`, readable and writable by you. sandbx checks before launching.

## Commands

```
sandbx <agent> [path]              # path defaults to cwd
sandbx <agent> [path] -- ARGS...   # pass ARGS to the agent
sandbx <agent> [path] -s|--shell   # bash in the sandbox; start the agent yourself
sandbx <agent> --dry-run           # print the podman command, run nothing
sandbx <agent> --no-relabel        # skip SELinux :z relabeling
sandbx build [agent]               # all agents; base only if missing
sandbx build --base                # rebuild the base image too
sandbx build --no-cache            # reinstall the agent CLIs (see Updating)
sandbx list
sandbx reset <agent> [-y]          # delete that agent's state and credentials
```

Agents: `claude`, `codex`, `copilot`, `opencode`, `vibe` (Mistral Vibe).

## Guarantees

| | How |
| --- | --- |
| No host access beyond the project | Exactly two bind mounts: the project and that agent's state dir |
| Agents can't read each other's credentials | Other agents' state dirs are not mounted at all |
| Sandbox is disposable, data survives | `--rm`, bind mounts only |
| A kernel exploit stays in the guest | Each sandbox is a libkrun microVM (`--runtime=krun`) with its own kernel |
| Host network only through reserved ports | pasta forwards the ranges under [Ports](#ports), nothing else |

The agent still reaches the internet and your LAN. It doesn't reach host
localhost services outside the reserved ports, or the host's abstract unix
sockets (X11 and similar). The project mount stays read-write, so review what
the agent leaves there (git hooks, `.envrc`, task files) before running it on
the host.

## Ports

| Host ports | Direction | Use |
| --- | --- | --- |
| 30000–30009 | sandbox → host, shared by all sandboxes | 30000 adb server, 30001 Burp proxy, 30002 Burp MCP, 30003–30009 as needed |
| 30010–30099 | host → sandbox, host `127.0.0.1` only | One block of 10 per sandbox, always seen inside as 30010–30019 |

**Sandbox → host:** inside the VM, `127.0.0.1:30000`–`30009` reach the same
ports on the host's localhost. Point host services there:

```sh
export ANDROID_ADB_SERVER_PORT=30000   # host shell rc; every host adb must use it
adb start-server
```

In Burp, add proxy listeners on `127.0.0.1:30001` (and `30002` for the MCP
server) next to whatever your phone uses. sandbx sets `ANDROID_ADB_SERVER_PORT`
inside, so `adb devices` there talks to the host server. The client and server
versions must match, or the client kills your host server. `adb forward` opens
its listener on the host, so forward to a port in this range, e.g.
`adb forward tcp:30003 tcp:27042` then `frida -H 127.0.0.1:30003`.

**Host → sandbox:** a server the agent starts on 30010–30019 appears on the
host's `127.0.0.1`. Each sandbox takes the first free block of 30010–30099 on
launch (up to nine at once), prints it, and exposes it inside as
`$SANDBX_HOST_PORTS`. The ranges must be free on the host:
`ss -tln '( sport >= :30000 and sport <= :30099 )'`.

## State and logins

```
~/.local/share/sandbx/agents/
├── claude/    -> /home/agent/.claude    (CLAUDE_CONFIG_DIR)
├── codex/     -> /home/agent/.codex     (CODEX_HOME)
├── copilot/   -> /home/agent/.copilot   (COPILOT_HOME)
├── opencode/  -> /home/agent/.opencode  (XDG_{CONFIG,DATA,STATE,CACHE}_HOME)
└── vibe/      -> /home/agent/.vibe      (VIBE_HOME)
```

## Project path inside the container

The project is mounted at `/workspace/<name>`, where `<name>` is its basename
(unsafe characters become `-`). This is readable, hides your host layout, and
survives moving the project.

## Details

- **Refused paths:** `/`, `$HOME`, anything containing or inside the state dir,
  and any path with `:` (podman's `-v` can't escape it). Symlinks are resolved first.
- **SELinux:** mounts use `:z`, which relabels the project to `container_file_t`
  on the host. `:Z` would relabel on every launch. `--no-relabel` disables label
  confinement instead.
- **Container user:** `agent`, uid 1000, via `--userns=keep-id:uid=1000,gid=1000`,
  so files come out owned by you. Passwordless `sudo`; installs vanish on exit.
  The VM itself starts as root in the user namespace (`--user 0:0`), because
  krun's VMM opens every file for the guest and as `agent` it can't open
  root-owned ones, which breaks `sudo`. `setpriv` drops the agent (or the
  `--shell` shell) to `agent` before it starts.
- **VM size:** 4 GiB RAM per sandbox (libkrun's default is 1 GiB), committed
  only as used. vCPUs default to every CPU sandbx may run on, up to 16.
- **Shell first:** `--shell` builds the identical sandbox (same mounts and env)
  but runs `bash -l` instead of the agent. Set things up, then run `claude` (etc.)
  by hand in the same container; it all goes away when the shell exits. Can't
  be combined with `-- ARGS` — pass those on the agent's own command line.


## Updating an agent

The CLIs live in the image, not in a mount, so an in-container `npm update -g`
or self-update is discarded on exit. Rebuilding alone isn't enough either: each
install is one `RUN` layer, so podman reuses the cached one and the version
never moves. `--no-cache` is what forces the reinstall:

```sh
sandbx build claude --no-cache                 # one agent
sandbx build --no-cache                        # all agents
podman run --rm sandbx-claude claude --version # confirm
```

The base image is left alone unless it's missing, since it changes rarely and
takes minutes. Rebuild it deliberately:

```sh
sandbx build --base --no-cache --pull          # base (+ ubuntu:24.04) and agents
```

`--pull` only affects the base; agent images build `FROM sandbx-base`. `--no-base`
skips the base even when it's missing. Old layers stay on disk — `podman image
prune` reclaims the space.

## Install / uninstall

`install.sh` symlinks rather than copies, so keep this directory in place (re-run
it if you move it). It won't overwrite an unrelated `sandbx` without `--force`.

```sh
./install.sh --prefix /usr/local/bin
./uninstall.sh                     # remove the symlink only
./uninstall.sh --purge --images    # also delete all state and images
```
