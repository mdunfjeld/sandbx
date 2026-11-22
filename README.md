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
- crun with `krun.use_passt` support (2026-04 or later) and a libkrun whose
  `krun_add_net_unixstream` can run DHCP in the guest
- passt on the host: crun starts the host's `/usr/bin/passt` for the VM, not
  one from the image. Podman's pasta is the same package.
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
sandbx <agent> -p work             run with one of the agent's profiles

sandbx build [agent]               build images (base only if missing)
sandbx build --base --no-cache     rebuild base from scratch
sandbx list                        show agents, images, profiles
sandbx reset <agent> [-y]          delete the agent's active profile's state
sandbx reset <agent> --all-profiles

sandbx profile list [agent]
sandbx profile create <agent> <name>
sandbx profile rename <agent> <old> <new>
sandbx profile delete <agent> <name> [-y]
sandbx profile set <agent> <name>  set the agent's active profile
```

Every launch prints a box with the agent, profile, runtime and port block;
`--shell` adds how to start the agent from the shell.

Agents: `claude`, `codex`, `copilot`, `opencode`, `vibe`.

## Profiles

Profiles let you maintain separate login sessions per agent — e.g. personal
and work credentials. Each agent has its own profiles and its own active
profile; creating, renaming or deleting a claude profile never touches codex.

```
~/.local/share/sandbx/
  profiles.json   # per agent: its profiles and which one is active
  agents/
    claude/
      default/    # mounted when claude runs with profile=default
      work/       # mounted when claude runs with profile=work
    codex/
      default/
```

Every agent starts with a `default` profile. After `sandbx profile create
claude work` and `sandbx profile set claude work`, `sandbx claude .` uses the
work profile without `-p work`. `-p` only accepts a profile the agent already
has, so a typo fails instead of starting a fresh, logged-out session.

A `profiles.json` from before per-agent profiles is migrated on first use:
each agent keeps `default`, its active profile and any profile with state on
disk, listed or not. The old file is kept as `profiles.v1.json`. Without a
`profiles.json`, an agent's state directories are its profiles.

`sandbx profile create` on a name that already has state on disk takes that
state over, credentials included, and says so. Run `sandbx reset <agent> -p
<name>` to empty it.

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
| 30000–30009 | sandbox → host (shared), `--runtime crun` only | adb 30000, Burp proxy 30001, Burp MCP 30002, 30003–30009 free |
| 30010–30099 | host → sandbox (`127.0.0.1`) | One block of 10 per sandbox, same port inside and out |

In a `--runtime crun` container, `127.0.0.1:30000`–`30009` reach the host's
localhost. The host adb server only has to run on 30000:

```sh
adb kill-server; ANDROID_ADB_SERVER_PORT=30000 adb start-server
```

Each sandbox claims the first free 10-port block from 30010–30099 and prints
it; `$SANDBX_HOST_PORTS` has the range. A server on those ports appears on the
host's `127.0.0.1` at the same numbers.

### Networking in a VM

A krun VM has a virtio-net NIC served by passt (`krun.use_passt`), not
libkrun's default TSI, which proxies every guest socket through the VMM. TSI
holds a socket's lock through a blocking `recv()`, so a program that reads on
one thread and writes on another hangs: `adb shell` froze on the first key.

A VM gets no sandbox → host ports. crun starts passt in the container's
network namespace with fixed options: `-t all` takes every free port there, so
pasta's 30000–30009 listeners could not bind (`Address already in use`), and
`--no-map-gw` leaves the guest no route to them anyway.

Inbound still works, but connections arrive at the guest's `eth0` address,
not its loopback. A server in the VM must listen on `0.0.0.0` (e.g.
`--host 0.0.0.0`) for the host to reach it.

## Updating

Agent images always skip the build cache, so a plain rebuild pulls the latest
CLI version:

```sh
sandbx build claude                     # rebuild one agent
sandbx build                            # rebuild all agents
sandbx build --base --no-cache --pull   # also rebuild base + ubuntu:26.04
```

`--no-cache` and `--pull` only affect the base image. Old layers stay on
disk — `podman image prune` reclaims space.

## Install / uninstall

`install.sh` symlinks into `~/.local/bin` (re-run if you move the repo).

```sh
./install.sh --prefix /usr/local/bin
./uninstall.sh                          # remove the symlink
./uninstall.sh --purge --images         # also delete state and images
```
