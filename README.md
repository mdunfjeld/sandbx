# sandbx

Run an AI coding agent in a disposable podman container where the only access to the host filesystem is the selected project
directory and that agent's own credentials — nothing else.

```sh
./install.sh                   # symlink sandbx into ~/.local/bin
sandbx build                   # build all images (once)
sandbx claude ~/project        # launch
```

The container is deleted on exit; the agent's config and session history persist.

## Commands

```
sandbx <agent> [path]              # path defaults to cwd
sandbx <agent> [path] -- ARGS...   # pass ARGS to the agent
sandbx <agent> --dry-run           # print the podman command, run nothing
sandbx <agent> --no-relabel        # skip SELinux :z relabeling
sandbx build [agent]               # all agents; base only if missing
sandbx build --base                # rebuild the base image too
sandbx build --no-cache            # reinstall the agent CLIs (see Updating)
sandbx list
sandbx reset <agent> [-y]          # delete that agent's state and credentials
```

Agents: `claude`, `codex`, `copilot`, `opencode`.

## Guarantees

| | How |
| --- | --- |
| No host access beyond the project | Exactly two bind mounts: the project and that agent's state dir |
| Agents can't read each other's credentials | Other agents' state dirs are not mounted at all |
| Sandbox is disposable, data survives | `--rm`, bind mounts only |

Networking is `--network=host`, so an agent reaches host localhost services and
the LAN. The kernel is shared. This limits blast radius; it is not a boundary
against a kernel exploit.

## State and logins

```
~/.local/share/sandbx/agents/
├── claude/    -> /home/agent/.claude    (CLAUDE_CONFIG_DIR)
├── codex/     -> /home/agent/.codex     (CODEX_HOME)
├── copilot/   -> /home/agent/.copilot   (COPILOT_HOME)
└── opencode/  -> /home/agent/.opencode  (XDG_{CONFIG,DATA,STATE,CACHE}_HOME)
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
