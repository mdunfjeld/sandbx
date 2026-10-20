# sandbx

Run AI coding agents in disposable libkrun microVMs via podman. Each agent sees
only the project directory and its own credentials — nothing else.
`--runtime crun` runs a plain container instead, for hosts without KVM.

```sh
./install.sh              # symlink into ~/.local/bin
sandbx build              # build all images (once)
sandbx claude ~/project   # launch
```

The VM is deleted on exit; credentials and session history persist via bind mounts.
Several sandboxes run at once.

## Requirements

- podman with `krun`: `sudo dnf install crun-krun` on Fedora
- `/dev/kvm` readable and writable by you

`--runtime crun` needs neither, only podman.

## Commands

```
sandbx <agent> [path]              run agent (path defaults to cwd)
sandbx <agent> [path] -- ARGS      pass ARGS to the agent
sandbx <agent> [path] -s|--shell   bash in the sandbox, start agent yourself
sandbx <agent> --dry-run           print the podman command, run nothing
sandbx <agent> --no-relabel        skip SELinux :z relabeling
sandbx <agent> --ram 8G --cpus 4   size the VM (krun only)
sandbx <agent> --runtime crun      plain container, no VM
sandbx <agent> -p work             run with a named profile

sandbx build [agent]               build images (base only if missing)
sandbx build --base --no-cache     rebuild base + agents from scratch
sandbx list                        show agents, images, profiles
sandbx reset <agent> [-y]          delete active profile's state
sandbx reset <agent> --all-profiles

sandbx profile list
sandbx profile create <name>
sandbx profile rename <old> <new>
sandbx profile delete <name> [-y]
sandbx profile set <name>          set the global default profile
sandbx profile set <name> --agent claude   per-agent default
```

Agents: `claude`, `codex`, `copilot`, `opencode`, `vibe`.

## Profiles

Profiles let you maintain separate login sessions per agent — e.g. personal
and work credentials.

```
~/.local/share/sandbx/
  profiles.json
  agents/
    claude/
      default/    # mounted when profile=default
      work/       # mounted when profile=work
    codex/
      default/
```

A `default` profile is created automatically. Use `sandbx profile set work
--agent claude` so `sandbx claude .` always uses the work profile without
`-p work`.

## Isolation guarantees

| Guarantee | Mechanism |
| --- | --- |
| No host access beyond the project | Exactly two bind mounts: project + agent state |
| Agents can't read each other's credentials | Only the running agent's state dir is mounted |
| Sandbox is disposable, data survives | `--rm` container, bind mounts persist |
| Kernel exploit stays in guest | libkrun microVM with its own kernel (not with `--runtime crun`) |
| Host network only through reserved ports | pasta forwards specific ranges, nothing else |

The agent still reaches the internet and your LAN. Review what it leaves in the
project (git hooks, `.envrc`, task files) before running on the host.

## Ports

| Host ports | Direction | Purpose |
| --- | --- | --- |
| 30000–30009 | sandbox → host (shared) | adb 30000, Burp proxy 30001, Burp MCP 30002, 30003–30009 free |
| 30010–30099 | host → sandbox (`127.0.0.1`) | One block of 10 per sandbox, same port inside and out |

Inside the VM, `127.0.0.1:30000`–`30009` reach the host's localhost. Each
sandbox claims the first free 10-port block from 30010–30099 and prints it;
`$SANDBX_HOST_PORTS` has the range. A server on those ports appears on the
host's `127.0.0.1` at the same numbers.

## Updating

Agent CLIs live in the image. Rebuilding alone reuses the cached layer — use
`--no-cache` to force a reinstall:

```sh
sandbx build claude --no-cache          # one agent
sandbx build --no-cache                 # all agents
sandbx build --base --no-cache --pull   # also rebuild base + ubuntu:26.04
```

`--pull` only affects the base image. Old layers stay on disk — `podman image
prune` reclaims space.

## Install / uninstall

`install.sh` symlinks into `~/.local/bin` (re-run if you move the repo).

```sh
./install.sh --prefix /usr/local/bin
./uninstall.sh                          # remove the symlink
./uninstall.sh --purge --images         # also delete state and images
```
