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
sandbx build [agent] [--no-base]   # default: all images
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


## Install / uninstall

`install.sh` symlinks rather than copies, so keep this directory in place (re-run
it if you move it). It won't overwrite an unrelated `sandbx` without `--force`.

```sh
./install.sh --prefix /usr/local/bin
./uninstall.sh                     # remove the symlink only
./uninstall.sh --purge --images    # also delete all state and images
```
