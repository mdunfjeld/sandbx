"""Tests for the podman argv builder and its guards.

The point of these is to make the isolation property an assertion rather than
something to re-verify by eye every time the registry changes. Nothing here
needs podman, so it runs anywhere.
"""

import contextlib
import importlib.machinery
import importlib.util
import io
import shutil
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "sandbx.py"
_spec = importlib.util.spec_from_loader(
    "sandbx", importlib.machinery.SourceFileLoader("sandbx", str(_SRC))
)
sandbx = importlib.util.module_from_spec(_spec)
# Register before exec: @dataclass resolves annotations via sys.modules.
sys.modules["sandbx"] = sandbx
_spec.loader.exec_module(sandbx)

AGENTS = sandbx.AGENTS
SandboxError = sandbx.SandboxError
build = sandbx.build_podman_args

HOME = Path("/home/tester")
AGENTS_ROOT = HOME / ".local/share/sandbx/agents"
PROJECT = Path("/home/tester/work/myproject")


def argv(name="claude", project=PROJECT, **kw):
    kw.setdefault("agents_root", AGENTS_ROOT)
    return build(name, project, **kw)


def mounts(args):
    """Every -v value, in order."""
    return [args[i + 1] for i, tok in enumerate(args) if tok == "-v"]


def skeleton(name, **kw):
    """The argv with this agent's own registry data substituted out.

    What is left is the shape the launcher builds regardless of which agent it
    was handed. If two agents produce different skeletons, some behavior depends
    on which agent you picked -- which is exactly what must not happen.
    """
    agent = AGENTS[name]
    subs = [
        (agent.image, "<image>"),
        (str(AGENTS_ROOT / agent.state_dir), "<state>"),
        (agent.config_path, "<config>"),
        (f"SANDBX_AGENT={name}", "SANDBX_AGENT=<name>"),
    ]
    for word in agent.command:
        subs.append((word, "<command>"))

    # An agent may need several env vars (opencode needs four). How many is
    # registry data, not behavior, so collapse the agent's own "-e K=V" pairs
    # into one placeholder at the position of the first.
    own_env = {f"{k}={v}" for k, v in agent.env.items()}
    raw = argv(name, **kw)
    out, i, placed = [], 0, False
    while i < len(raw):
        if raw[i] == "-e" and i + 1 < len(raw) and raw[i + 1] in own_env:
            if not placed:
                out += ["-e", "<agent-env>"]
                placed = True
            i += 2
            continue
        tok = raw[i]
        for old, new in sorted(subs, key=lambda p: -len(p[0])):
            tok = tok.replace(old, new)
        out.append(tok)
        i += 1
    return out


class TestAgentIndependence(unittest.TestCase):
    """Behavior must not depend on which agent was picked. The registry supplies
    names and paths; it must never change the shape of what gets built."""

    def test_argv_shape_is_identical_across_agents(self):
        shapes = {name: skeleton(name) for name in AGENTS}
        first = shapes[next(iter(shapes))]
        for name, shape in shapes.items():
            with self.subTest(agent=name):
                self.assertEqual(shape, first)

    def test_argv_shape_identical_under_every_option(self):
        for kw in [{}, {"relabel": False}, {"interactive": False},
                   {"extra": ["--resume"]}, {"shell": True},
                   {"relabel": False, "interactive": False},
                   {"shell": True, "relabel": False, "interactive": False}]:
            shapes = {name: skeleton(name, **kw) for name in AGENTS}
            first = shapes[next(iter(shapes))]
            for name, shape in shapes.items():
                with self.subTest(opts=kw, agent=name):
                    self.assertEqual(shape, first)

    def test_same_project_lands_at_the_same_path_for_every_agent(self):
        dests = {name: argv(name)[argv(name).index("-w") + 1] for name in AGENTS}
        self.assertEqual(len(set(dests.values())), 1, dests)

    def test_mount_count_and_order_are_identical(self):
        shapes = {name: [t for t in skeleton(name) if t in ("-v", "-w", "-e")]
                  for name in AGENTS}
        first = shapes[next(iter(shapes))]
        for name, shape in shapes.items():
            with self.subTest(agent=name):
                self.assertEqual(shape, first)


class TestRegistryValidation(unittest.TestCase):
    """A malformed registry entry must be a sentence, not a subtly wrong container."""

    def broken(self, **changes):
        import dataclasses
        bad = dataclasses.replace(AGENTS["claude"], **changes)
        return unittest.mock.patch.dict(AGENTS, {"claude": bad})

    def test_every_shipped_agent_validates(self):
        for name, agent in AGENTS.items():
            with self.subTest(agent=name):
                sandbx.validate_agent(name, agent)  # must not raise

    def test_rejects_missing_image(self):
        with self.broken(image=""):
            with self.assertRaises(SandboxError):
                argv("claude")

    def test_rejects_missing_command(self):
        with self.broken(command=()):
            with self.assertRaises(SandboxError):
                argv("claude")

    def test_rejects_state_dir_with_a_separator(self):
        """'../codex' would climb out of this agent's own state directory."""
        for bad in ("../codex", "a/b", "", "."):
            with self.subTest(state_dir=bad):
                with self.broken(state_dir=bad):
                    with self.assertRaises(SandboxError):
                        argv("claude")

    def test_rejects_relative_config_path(self):
        with self.broken(config_path=".claude"):
            with self.assertRaises(SandboxError):
                argv("claude")

    def test_rejects_env_path_outside_the_mount(self):
        with self.broken(env={"CLAUDE_CONFIG_DIR": "/home/agent/elsewhere"}):
            with self.assertRaises(SandboxError):
                argv("claude")

    def test_rejects_duplicate_state_dir(self):
        """Two agents sharing a state dir would share credentials."""
        with self.broken(state_dir="codex"):
            with self.assertRaises(SandboxError):
                argv("claude")

    def test_rejects_duplicate_config_path(self):
        with self.broken(config_path=AGENTS["codex"].config_path):
            with self.assertRaises(SandboxError):
                argv("claude")

    def test_validate_registry_checks_every_agent(self):
        """Not just the one being acted on -- that is how cmd_reset slipped."""
        sandbx.validate_registry()  # the shipped registry must pass
        with self.broken(state_dir="codex"):
            with self.assertRaises(SandboxError):
                sandbx.validate_registry()

    def test_every_command_path_is_covered(self):
        """main() validates before dispatch, so no subcommand can skip it."""
        for args in (["list"], ["build"], ["reset", "claude"], ["claude", "."]):
            with self.subTest(argv=args):
                with self.broken(state_dir="codex"):
                    with self.assertRaises(SandboxError):
                        sandbx.main(args)


class TestIsolation(unittest.TestCase):
    def test_agent_never_references_another_agent(self):
        """The core guarantee: no path or flag from one agent leaks into another."""
        for name in AGENTS:
            rendered = " ".join(argv(name))
            for other in AGENTS:
                if other == name:
                    continue
                with self.subTest(agent=name, other=other):
                    self.assertNotIn(other, rendered)

    def test_exactly_two_mounts(self):
        """One config mount, one project mount. Anything else is a hole."""
        for name in AGENTS:
            with self.subTest(agent=name):
                self.assertEqual(len(mounts(argv(name))), 2)

    def test_config_mount_is_scoped_to_this_agent(self):
        for name, agent in AGENTS.items():
            with self.subTest(agent=name):
                config_mount = mounts(argv(name))[0]
                self.assertTrue(
                    config_mount.startswith(f"{AGENTS_ROOT / agent.state_dir}:"),
                    config_mount,
                )
                self.assertIn(agent.config_path, config_mount)

    def test_no_host_home_mount(self):
        for name in AGENTS:
            for mount in mounts(argv(name)):
                with self.subTest(agent=name, mount=mount):
                    self.assertFalse(mount.startswith(f"{HOME}:"))


class TestProjectMount(unittest.TestCase):
    def test_project_is_the_mount_source(self):
        """The host path is what gets mounted; where it lands is workdir's call."""
        args = argv()
        sources = [m.split(":")[0] for m in mounts(args)]
        self.assertIn(str(PROJECT), sources)

    def test_workdir_matches_the_mount_destination(self):
        """-w and the project mount's destination must not drift apart."""
        for name in AGENTS:
            with self.subTest(agent=name):
                args = argv(name)
                dest = mounts(args)[1].rsplit(":", 1)[0].split(":", 1)[1]
                self.assertEqual(args[args.index("-w") + 1], dest)

    def test_distinct_projects_get_distinct_paths(self):
        other = Path("/home/tester/work/another")
        self.assertNotEqual(
            argv(project=PROJECT)[argv(project=PROJECT).index("-w") + 1],
            argv(project=other)[argv(project=other).index("-w") + 1],
        )


class TestFlags(unittest.TestCase):
    def test_ephemeral_and_host_network(self):
        args = argv()
        self.assertIn("--rm", args)
        self.assertIn("--network=host", args)

    def test_userns_pins_uid(self):
        self.assertIn("--userns=keep-id:uid=1000,gid=1000", argv())

    def test_relabel_default_on(self):
        for mount in mounts(argv()):
            self.assertTrue(mount.endswith(":z"), mount)

    def test_no_relabel_drops_suffix_and_disables_label(self):
        args = argv(relabel=False)
        for mount in mounts(args):
            self.assertFalse(mount.endswith(":z"), mount)
        self.assertIn("label=disable", args)

    def test_no_automode_flag_is_ever_added(self):
        """sandbx does not offer a one-word way into an agent's automode. The
        flags still exist upstream and `-- <flag>` still reaches them; what is
        gone is sandbx appending one for you."""
        automode_flags = [
            "--dangerously-skip-permissions",          # claude
            "--dangerously-bypass-approvals-and-sandbox",  # codex
            "--allow-all-tools",                       # copilot
            "--auto-approve", "--yolo",                # vibe
        ]
        for name in AGENTS:
            rendered = " ".join(argv(name))
            for flag in automode_flags:
                with self.subTest(agent=name, flag=flag):
                    self.assertNotIn(flag, rendered)

    def test_registry_carries_no_automode_flags(self):
        """The field is gone; a re-added one would resurface the convenience."""
        for name, agent in AGENTS.items():
            with self.subTest(agent=name):
                self.assertFalse(hasattr(agent, "auto_flags"))

    def test_run_parser_rejects_auto(self):
        """`sandbx claude . --auto` must fail, not be quietly ignored."""
        with self.assertRaises(SystemExit):
            sandbx.cmd_run("claude", ["--auto", "--dry-run"])

    def test_config_env_var_is_set(self):
        for name, agent in AGENTS.items():
            args = argv(name)
            for key, value in agent.env.items():
                with self.subTest(agent=name, key=key):
                    self.assertIn(f"{key}={value}", args)

    def test_passthrough_lands_after_the_command(self):
        args = argv(extra=["--resume"])
        self.assertEqual(args[-1], "--resume")
        self.assertGreater(args.index("--resume"), args.index("sandbx-claude"))

    def test_unknown_agent_rejected(self):
        with self.assertRaises(SandboxError):
            argv("gemini")


class TestShellMode(unittest.TestCase):
    """--shell must yield the very same sandbox; only the command differs."""

    def run_cli(self, name, args, *, real=False):
        """cmd_run against a temp project and state root; returns (rc, out, err).

        real=True goes past --dry-run with podman and execvp faked out.
        """
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "proj"
            project.mkdir()
            env = {"HOME": str(Path(tmp) / "home"),
                   "XDG_DATA_HOME": str(Path(tmp) / "data")}
            out, err = io.StringIO(), io.StringIO()
            with contextlib.ExitStack() as stack:
                stack.enter_context(unittest.mock.patch.dict(sandbx.os.environ, env))
                stack.enter_context(contextlib.redirect_stdout(out))
                stack.enter_context(contextlib.redirect_stderr(err))
                execvp = None
                if real:
                    stack.enter_context(unittest.mock.patch.object(
                        sandbx.shutil, "which", return_value="/usr/bin/podman"))
                    execvp = stack.enter_context(
                        unittest.mock.patch.object(sandbx.os, "execvp"))
                rc = sandbx.cmd_run(name, [str(project), *args])
            self.execvp = execvp
            return rc, out.getvalue(), err.getvalue()

    def test_runs_a_login_shell_instead_of_the_agent(self):
        for name, agent in AGENTS.items():
            with self.subTest(agent=name):
                args = argv(name, shell=True)
                tail = args[args.index(agent.image) + 1:]
                self.assertEqual(tail, list(sandbx.SHELL_COMMAND))

    def test_sandbox_is_identical_up_to_the_image(self):
        for name, agent in AGENTS.items():
            for kw in [{}, {"relabel": False}, {"interactive": False}]:
                with self.subTest(agent=name, opts=kw):
                    normal = argv(name, **kw)
                    shell = argv(name, shell=True, **kw)
                    cut = normal.index(agent.image) + 1
                    self.assertEqual(shell[:cut], normal[:cut])

    def test_still_exactly_two_mounts_and_agent_marker(self):
        for name in AGENTS:
            with self.subTest(agent=name):
                args = argv(name, shell=True)
                self.assertEqual(len(mounts(args)), 2)
                self.assertIn(f"SANDBX_AGENT={name}", args)

    def test_builder_rejects_passthrough(self):
        with self.assertRaises(SandboxError):
            argv(shell=True, extra=["--resume"])

    def test_cli_rejects_passthrough(self):
        with self.assertRaises(SystemExit):
            self.run_cli("claude", ["--shell", "--dry-run", "--", "--resume"])

    def test_dry_run_prints_the_shell_command_and_no_hint(self):
        for flag in ("--shell", "-s"):
            with self.subTest(flag=flag):
                rc, out, err = self.run_cli("claude", [flag, "--dry-run"])
                self.assertEqual(rc, 0)
                self.assertTrue(out.strip().endswith("sandbx-claude /bin/bash -l"), out)
                self.assertEqual(err, "")

    def test_real_run_prints_a_hint_and_execs_the_shell(self):
        for name, agent in AGENTS.items():
            with self.subTest(agent=name):
                _, _, err = self.run_cli(name, ["--shell"], real=True)
                self.assertIn(f"run `{agent.command[0]}`", err)
                args = self.execvp.call_args.args[1]
                self.assertEqual(args[-2:], list(sandbx.SHELL_COMMAND))

    def test_normal_run_prints_no_hint(self):
        _, _, err = self.run_cli("claude", [], real=True)
        self.assertEqual(err, "")
        self.assertEqual(self.execvp.call_args.args[1][-1], "claude")


class TestGuards(unittest.TestCase):
    def check(self, project):
        sandbx.validate_project(Path(project), HOME, AGENTS_ROOT)

    def test_allows_ordinary_project(self):
        self.check(PROJECT)  # must not raise

    def test_rejects_filesystem_root(self):
        with self.assertRaises(SandboxError):
            self.check("/")

    def test_rejects_home(self):
        with self.assertRaises(SandboxError):
            self.check(HOME)

    def test_rejects_ancestor_of_state_dir(self):
        """Mounting ~/.local/share would hand over every agent's credentials."""
        with self.assertRaises(SandboxError):
            self.check(HOME / ".local/share")

    def test_rejects_state_dir_itself(self):
        with self.assertRaises(SandboxError):
            self.check(AGENTS_ROOT)

    def test_rejects_inside_state_dir(self):
        with self.assertRaises(SandboxError):
            self.check(AGENTS_ROOT / "claude")

    def test_rejects_relative_path(self):
        with self.assertRaises(SandboxError):
            self.check("relative/path")


class TestWorkdir(unittest.TestCase):
    """Per-agent choice between mirroring the host path and a fixed mountpoint."""

    def dest(self, name):
        """Destination half of the project mount (mounts[1]), minus any :z."""
        return mounts(argv(name))[1].split(":")[1]

    def test_every_agent_uses_the_same_rule(self):
        """The point of "identical everywhere": one project, one container path,
        whichever agent you launch."""
        dests = {name: self.dest(name) for name in AGENTS}
        self.assertEqual(len(set(dests.values())), 1, dests)

    def test_no_agent_opts_out(self):
        """A registry entry reverting to mirroring would reintroduce the split."""
        for name, agent in AGENTS.items():
            with self.subTest(agent=name):
                self.assertEqual(agent.workdir, sandbx.WORKDIR_TEMPLATE)

    def test_derived_workdir_agents_use_it_for_mount_and_cwd(self):
        for name, agent in AGENTS.items():
            with self.subTest(agent=name):
                args = argv(name)
                expected = sandbx.workdir_for(agent, PROJECT)
                self.assertEqual(self.dest(name), expected)
                self.assertEqual(args[args.index("-w") + 1], expected)

    def test_fixed_workdir_hides_the_host_path(self):
        """The whole point: the container must not see where this came from."""
        for name, agent in AGENTS.items():
            if agent.workdir is None:
                continue
            with self.subTest(agent=name):
                args = argv(name)
                # The host path may appear only as the source half of the
                # project mount -- not as a cwd, an env var, or anywhere else.
                leaks = [tok for tok in args
                         if str(PROJECT) in tok
                         and not tok.startswith(f"{PROJECT}:")]
                self.assertEqual(leaks, [])

    def test_copilot_uses_the_derived_template(self):
        self.assertEqual(AGENTS["copilot"].workdir, sandbx.WORKDIR_TEMPLATE)
        self.assertEqual(self.dest("copilot"), "/workspace/myproject")

    def test_no_agent_exposes_the_host_path(self):
        """The original complaint: `pwd` in the container must not echo the host."""
        for name in AGENTS:
            with self.subTest(agent=name):
                self.assertNotIn(str(PROJECT), self.dest(name))

    def test_fixed_workdir_still_yields_exactly_two_mounts(self):
        self.assertEqual(len(mounts(argv("copilot"))), 2)

    def test_workdirs_are_absolute(self):
        for name, agent in AGENTS.items():
            with self.subTest(agent=name):
                self.assertTrue(self.dest(name).startswith("/"), self.dest(name))

    def test_relabel_suffix_applies_to_a_derived_workdir(self):
        dest = sandbx.workdir_for(AGENTS["copilot"], PROJECT)
        self.assertIn(f"{PROJECT}:{dest}:z", mounts(argv("copilot")))
        for mount in mounts(argv("copilot", relabel=False)):
            self.assertFalse(mount.endswith(":z"), mount)


class TestDerivedWorkdir(unittest.TestCase):
    """Name-only workdir: readable and stable, but not unique across same-named
    projects -- a deliberate trade-off."""

    AGENT = AGENTS["copilot"]

    def render(self, path):
        return sandbx.workdir_for(self.AGENT, Path(path))

    def test_stable_across_runs(self):
        """Same project, same path -- or --continue resumes nothing."""
        self.assertEqual(self.render(PROJECT), self.render(PROJECT))

    def test_different_names_differ(self):
        self.assertNotEqual(
            self.render("/home/tester/work/api"),
            self.render("/home/tester/work/web"),
        )

    def test_survives_moving_the_project(self):
        """The reason for name-only: history keys don't change on a move."""
        self.assertEqual(
            self.render("/home/tester/work/api"),
            self.render("/srv/elsewhere/api"),
        )

    def test_same_basename_collides_by_design(self):
        """Pins the accepted trade-off so a change to it is deliberate."""
        self.assertEqual(
            self.render("/home/tester/a/api"),
            self.render("/home/tester/b/api"),
        )

    def test_hides_the_host_layout(self):
        dest = self.render("/home/tester/deeply/nested/myproject")
        self.assertNotIn("tester", dest)
        self.assertNotIn("nested", dest)
        self.assertTrue(dest.startswith("/workspace/"), dest)

    def test_keeps_the_basename_readable(self):
        self.assertEqual(self.render(PROJECT), "/workspace/myproject")

    def test_sanitizes_awkward_basenames(self):
        """A ':' would corrupt the -v src:dst spec; spaces are just unpleasant."""
        for raw, expect in [
            ("/tmp/a:b", "a-b"),
            ("/tmp/my project", "my-project"),
            ("/tmp/.hidden", "hidden"),
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(self.render(raw), f"/workspace/{expect}")

    def test_unnameable_basename_falls_back(self):
        self.assertEqual(self.render("/tmp/..."), "/workspace/project")

    def test_none_workdir_mirrors(self):
        """The escape hatch, unused by the registry but kept working."""
        import dataclasses
        mirrored = dataclasses.replace(self.AGENT, workdir=None)
        self.assertEqual(sandbx.workdir_for(mirrored, PROJECT), str(PROJECT))

    def test_bad_template_is_a_clean_error(self):
        import dataclasses
        broken = dataclasses.replace(self.AGENT, workdir="/workspace/{nope}")
        with self.assertRaises(SandboxError):
            sandbx.workdir_for(broken, PROJECT)

    def test_plain_string_template_is_still_supported(self):
        import dataclasses
        fixed = dataclasses.replace(self.AGENT, workdir="/workspace")
        self.assertEqual(sandbx.workdir_for(fixed, PROJECT), "/workspace")


class TestWorkdirGuards(unittest.TestCase):
    """build_podman_args rejects a registry entry that would defeat a mount."""

    def with_workdir(self, workdir):
        import dataclasses
        broken = dataclasses.replace(AGENTS["copilot"], workdir=workdir)
        return unittest.mock.patch.dict(AGENTS, {"copilot": broken})

    def test_rejects_relative_workdir(self):
        with self.with_workdir("workspace"):
            with self.assertRaises(SandboxError):
                argv("copilot")

    def test_rejects_workdir_equal_to_config_path(self):
        with self.with_workdir(AGENTS["copilot"].config_path):
            with self.assertRaises(SandboxError):
                argv("copilot")

    def test_rejects_workdir_containing_config_path(self):
        """Mounting the project at /home/agent would bury the config mount."""
        with self.with_workdir("/home/agent"):
            with self.assertRaises(SandboxError):
                argv("copilot")


class TestColonPaths(unittest.TestCase):
    """A ':' in a path has no escape in podman's -v syntax, so it must be a
    clean refusal rather than a quietly reshaped mount table."""

    def test_mirrored_agent_rejects_a_colon_project(self):
        with self.assertRaises(SandboxError):
            argv("claude", Path("/home/tester/work/a:b"))

    def test_derived_agent_rejects_a_colon_project(self):
        """The template sanitizes the destination, but the source still has one."""
        with self.assertRaises(SandboxError):
            argv("copilot", Path("/home/tester/work/a:b"))

    def test_every_agent_rejects_it(self):
        for name in AGENTS:
            with self.subTest(agent=name):
                with self.assertRaises(SandboxError):
                    argv(name, Path("/home/tester/a:b"))

    def test_colon_in_the_state_root_is_caught_too(self):
        with self.assertRaises(SandboxError):
            argv("claude", agents_root=Path("/mnt/disk:1/sandbx/agents"))

    def test_error_names_the_offending_path(self):
        with self.assertRaises(SandboxError) as ctx:
            argv("claude", Path("/home/tester/work/a:b"))
        self.assertIn("a:b", str(ctx.exception))

    def test_ordinary_paths_are_unaffected(self):
        for mount in mounts(argv("claude")):
            self.assertEqual(mount.count(":"), 2)  # src:dst:z

    def test_commas_are_still_allowed(self):
        """-v takes commas fine; only ':' is unrepresentable."""
        project = Path("/home/tester/a,b")
        args = argv("claude", project)
        dest = sandbx.workdir_for(AGENTS["claude"], project)
        self.assertIn(f"{project}:{dest}:z", mounts(args))

    def test_mount_spec_builds_the_ordinary_case(self):
        self.assertEqual(sandbx.mount_spec("/src", "/dst", ":z"), "/src:/dst:z")
        self.assertEqual(sandbx.mount_spec("/src", "/dst", ""), "/src:/dst")


class TestAgentEnv(unittest.TestCase):
    def test_env_paths_live_inside_the_mount(self):
        """A path outside config_path is written to the container and lost."""
        for name, agent in AGENTS.items():
            for key, value in agent.env.items():
                with self.subTest(agent=name, var=key):
                    self.assertTrue(
                        value == agent.config_path
                        or value.startswith(agent.config_path + "/"),
                        f"{key}={value} is outside {agent.config_path}",
                    )

    def test_opencode_gets_all_four_xdg_dirs(self):
        """auth.json lives under XDG_DATA_HOME; missing any one leaks state."""
        env = AGENTS["opencode"].env
        for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
            with self.subTest(var=var):
                self.assertIn(var, env)
                self.assertIn(f"{var}={env[var]}", argv("opencode"))

    def test_vibe_home_is_the_hardcoded_default(self):
        """Some vibe paths ignore VIBE_HOME and write to ~/.vibe regardless; they
        persist only if the mount and VIBE_HOME are both exactly that path."""
        agent = AGENTS["vibe"]
        self.assertEqual(agent.config_path, f"{sandbx.CONTAINER_HOME}/.vibe")
        self.assertEqual(agent.env["VIBE_HOME"], agent.config_path)
        self.assertIn(f"VIBE_HOME={agent.config_path}", argv("vibe"))


class TestBuildFlags(unittest.TestCase):
    """The flags that decide whether a rebuild actually reinstalls anything."""

    CF = Path("/repo/images/Containerfile.claude")

    def cmd(self, **kw):
        return sandbx.build_image_cmd("sandbx-claude", self.CF, **kw)

    def test_plain_build_uses_the_cache(self):
        self.assertNotIn("--no-cache", self.cmd())
        self.assertNotIn("--pull", self.cmd())

    def test_flags_are_passed_through(self):
        self.assertIn("--no-cache", self.cmd(no_cache=True))
        self.assertIn("--pull", self.cmd(pull=True))

    def test_flags_precede_the_build_arguments(self):
        """podman build takes options before the context path."""
        cmd = self.cmd(no_cache=True, pull=True)
        self.assertEqual(cmd[:4], ["podman", "build", "--no-cache", "--pull"])
        self.assertEqual(cmd[-1], str(sandbx.IMAGES_DIR))
        self.assertEqual(cmd[cmd.index("-f") + 1], str(self.CF))
        self.assertEqual(cmd[cmd.index("-t") + 1], "sandbx-claude")

    def builds(self, args, base_exists=True):
        """Run cmd_build with podman stubbed; return the recorded build calls."""
        calls = []
        def record(tag, containerfile, *, no_cache=False, pull=False):
            calls.append((tag, no_cache, pull))
        with unittest.mock.patch.object(sandbx, "_build_image", record), \
             unittest.mock.patch.object(sandbx, "image_exists", lambda tag: base_exists):
            sandbx.cmd_build(args)
        return calls

    def tags(self, args, **kw):
        return [tag for tag, _, _ in self.builds(args, **kw)]

    def test_default_skips_the_base(self):
        """The base changes rarely; rebuilding it on every update wastes minutes."""
        self.assertNotIn(sandbx.BASE_IMAGE, self.tags([]))
        self.assertEqual(sorted(self.tags([])),
                         sorted(a.image for a in AGENTS.values()))

    def test_base_is_built_when_missing(self):
        """First build: agent images are FROM sandbx-base, so it must exist."""
        self.assertEqual(self.tags([], base_exists=False)[0], sandbx.BASE_IMAGE)

    def test_no_base_wins_even_when_missing(self):
        self.assertNotIn(sandbx.BASE_IMAGE, self.tags(["--no-base"], base_exists=False))

    def test_base_flag_forces_a_rebuild(self):
        self.assertEqual(self.tags(["--base"])[0], sandbx.BASE_IMAGE)

    def test_base_and_no_base_conflict(self):
        with self.assertRaises(SystemExit):
            self.builds(["--base", "--no-base"])

    def test_no_cache_reaches_every_image_built(self):
        calls = self.builds(["--base", "--no-cache"])
        self.assertTrue(all(no_cache for _, no_cache, _ in calls), calls)
        self.assertEqual(len(calls), len(AGENTS) + 1)   # base + every agent

    def test_pull_applies_to_the_base_only(self):
        """Agent images are FROM sandbx-base, so --pull is meaningless there."""
        calls = self.builds(["--base", "--pull"])
        pulled = [tag for tag, _, pull in calls if pull]
        self.assertEqual(pulled, [sandbx.BASE_IMAGE])

    def test_plain_build_passes_neither(self):
        for tag, no_cache, pull in self.builds([]):
            with self.subTest(image=tag):
                self.assertFalse(no_cache)
                self.assertFalse(pull)

    def test_flags_compose_with_agent_and_no_base(self):
        calls = self.builds(["claude", "--no-cache", "--no-base"])
        self.assertEqual(calls, [(AGENTS["claude"].image, True, False)])


class TestRegistry(unittest.TestCase):
    def test_state_dirs_are_unique(self):
        dirs = [a.state_dir for a in AGENTS.values()]
        self.assertEqual(len(dirs), len(set(dirs)))

    def test_config_paths_are_unique(self):
        paths = [a.config_path for a in AGENTS.values()]
        self.assertEqual(len(paths), len(set(paths)))

    def test_state_root_respects_xdg(self):
        self.assertEqual(
            sandbx.state_root({"XDG_DATA_HOME": "/x", "HOME": "/home/tester"}),
            Path("/x/sandbx/agents"),
        )
        self.assertEqual(
            sandbx.state_root({"HOME": "/home/tester"}),
            Path("/home/tester/.local/share/sandbx/agents"),
        )


class TestSymlinkedStateRoot(unittest.TestCase):
    """validate_project() compares the project against the state root, and
    resolve_project() resolves symlinks. If state_root() does not, the guard
    misses whenever a path component is a link -- ~/.local/share on its own
    disk, or /home -> /var/home on ostree systems."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # real/share/sandbx/agents, reached via a symlinked "home"
        self.real = self.tmp / "data" / "share"
        (self.real / "sandbx" / "agents" / "claude").mkdir(parents=True)
        self.home = self.tmp / "home" / "tester"
        (self.home / ".local").mkdir(parents=True)
        (self.home / ".local" / "share").symlink_to(self.real)

    def env(self):
        return {"HOME": str(self.home)}

    def test_state_root_is_resolved(self):
        self.assertEqual(
            sandbx.state_root(self.env()),
            (self.real / "sandbx" / "agents").resolve(),
        )

    def test_guard_still_rejects_the_state_dir_through_a_symlink(self):
        """The regression: reached by its real path, it must still be refused."""
        root = sandbx.state_root(self.env())
        project = sandbx.resolve_project(str(self.home / ".local/share/sandbx/agents/claude"))
        with self.assertRaises(SandboxError):
            sandbx.validate_project(project, sandbx.home_dir(self.env()), root)

    def test_guard_rejects_an_ancestor_reached_through_a_symlink(self):
        root = sandbx.state_root(self.env())
        project = sandbx.resolve_project(str(self.home / ".local/share"))
        with self.assertRaises(SandboxError):
            sandbx.validate_project(project, sandbx.home_dir(self.env()), root)

    def test_an_unrelated_project_is_still_allowed(self):
        ordinary = self.tmp / "work" / "myproject"
        ordinary.mkdir(parents=True)
        sandbx.validate_project(
            sandbx.resolve_project(str(ordinary)),
            sandbx.home_dir(self.env()),
            sandbx.state_root(self.env()),
        )  # must not raise


class TestHomeDir(unittest.TestCase):
    def test_missing_home_is_a_clean_error(self):
        for env in ({}, {"HOME": ""}):
            with self.subTest(env=env):
                with self.assertRaises(SandboxError):
                    sandbx.home_dir(env)

    def test_state_root_without_home_is_a_clean_error(self):
        with self.assertRaises(SandboxError):
            sandbx.state_root({})

    def test_xdg_alone_is_enough(self):
        """XDG_DATA_HOME short-circuits the HOME lookup entirely."""
        self.assertEqual(
            sandbx.state_root({"XDG_DATA_HOME": "/x"}), Path("/x/sandbx/agents")
        )


if __name__ == "__main__":
    unittest.main()
