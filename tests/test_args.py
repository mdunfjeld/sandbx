"""Tests for the podman argv builder and its guards.

The point of these is to make the isolation property an assertion rather than
something to re-verify by eye every time the registry changes. Nothing here
needs podman, so it runs anywhere.
"""

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import shlex
import shutil
import socket
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

# Every combination of builder options the shape and network tests sweep.
OPTION_SETS = [
    {}, {"relabel": False}, {"interactive": False},
    {"extra": ["--resume"]}, {"shell": True}, {"host_block": 30090},
    {"ram_mib": 8192, "cpus": 4},
    {"relabel": False, "interactive": False},
    {"shell": True, "relabel": False, "interactive": False},
    {"runtime": "crun"},
    {"runtime": "crun", "shell": True, "relabel": False, "interactive": False},
    {"profile": "work"},
]


def argv(name="claude", project=PROJECT, **kw):
    kw.setdefault("agents_root", AGENTS_ROOT)
    kw.setdefault("profile", "default")
    return build(name, project, **kw)


def mounts(args):
    """Every -v value, in order."""
    return [args[i + 1] for i, tok in enumerate(args) if tok == "-v"]


def parse_mount(mount_str):
    """Break a -v value into (src, dst, options)."""
    parts = mount_str.rsplit(":", 1)
    options = parts[1] if len(parts) == 2 and "/" not in parts[1] else ""
    body = parts[0] if options else mount_str
    src, dst = body.split(":", 1)
    return src, dst, options


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
        for kw in OPTION_SETS:
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
        for args in (["list"], ["build"], ["reset", "claude"], ["claude", "."],
                     ["profile", "list"]):
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
                    config_mount.startswith(
                        f"{AGENTS_ROOT / agent.state_dir / 'default'}:"
                    ),
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
                _, dest, _ = parse_mount(mounts(args)[1])
                self.assertEqual(args[args.index("-w") + 1], dest)

    def test_distinct_projects_get_distinct_paths(self):
        other = Path("/home/tester/work/another")
        self.assertNotEqual(
            argv(project=PROJECT)[argv(project=PROJECT).index("-w") + 1],
            argv(project=other)[argv(project=other).index("-w") + 1],
        )


class TestFlags(unittest.TestCase):
    def test_ephemeral_microvm(self):
        args = argv()
        self.assertIn("--rm", args)
        self.assertIn("--runtime=krun", args)

    def test_pasta_forwards_only_the_reserved_ranges(self):
        self.assertIn(
            "--network=pasta:-T,30000-30009,-t,127.0.0.1/30010-30019",
            argv(),
        )
        self.assertIn(
            "--network=pasta:-T,30000-30009,-t,127.0.0.1/30040-30049",
            argv(host_block=30040),
        )

    def test_never_host_network(self):
        """The regression guard: no option or agent brings --network=host back."""
        for kw in OPTION_SETS:
            for name in AGENTS:
                with self.subTest(agent=name, opts=kw):
                    nets = [t for t in argv(name, **kw) if t.startswith("--net")]
                    self.assertEqual(len(nets), 1, nets)
                    self.assertTrue(nets[0].startswith("--network=pasta:"), nets)

    def test_vm_runs_as_namespace_root(self):
        """As `agent`, the VMM cannot open root-owned files for the guest, and
        sudo fails with EACCES before it reads a line of its config."""
        args = argv()
        self.assertEqual(args[args.index("--user") + 1], "0:0")
        self.assertIn(f"HOME={sandbx.CONTAINER_HOME}", args)

    def test_workload_always_runs_as_agent(self):
        """Root is for the krun VMM only; whatever runs inside runs as agent.
        A VM drops there through AS_AGENT, a crun container starts there."""
        self.assertIn(f"--reuid={sandbx.CONTAINER_UID}", sandbx.AS_AGENT)
        self.assertIn(f"--regid={sandbx.CONTAINER_UID}", sandbx.AS_AGENT)
        for kw in OPTION_SETS:
            for name, agent in AGENTS.items():
                with self.subTest(agent=name, opts=kw):
                    args = argv(name, **kw)
                    start = args.index(agent.image) + 1
                    if kw.get("runtime") == "crun":
                        self.assertEqual(args[args.index("--user") + 1], "1000:1000")
                        self.assertNotIn("setpriv", args)
                    else:
                        self.assertEqual(
                            args[start:start + len(sandbx.AS_AGENT)],
                            list(sandbx.AS_AGENT),
                        )

    def test_vm_memory_is_set(self):
        """libkrun's 1 GiB default is too small; unset, builds run out of RAM."""
        args = argv()
        self.assertEqual(args[args.index("--annotation") + 1], "krun.ram_mib=4096")

    def test_adb_and_host_port_env(self):
        args = argv(host_block=30020)
        self.assertIn("ANDROID_ADB_SERVER_PORT=30000", args)
        self.assertIn("SANDBX_HOST_PORTS=30020-30029", args)

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


class CliRunner:
    def run_cli(self, name, args, *, real=False, kvm=True):
        """cmd_run against a temp project and state root; returns (rc, out, err).

        real=True goes past --dry-run with podman and execvp faked out. The
        host-facing probes are faked either way: the port block is always the
        first, and kvm decides whether /dev/kvm looks usable.
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
                stack.enter_context(unittest.mock.patch.object(
                    sandbx, "pick_host_block", return_value=sandbx.HOST_BLOCKS[0]))
                stack.enter_context(unittest.mock.patch.object(
                    sandbx, "have_kvm", return_value=kvm))
                self.execvp = None
                if real:
                    stack.enter_context(unittest.mock.patch.object(
                        sandbx.shutil, "which", return_value="/usr/bin/podman"))
                    self.execvp = stack.enter_context(
                        unittest.mock.patch.object(sandbx.os, "execvp"))
                rc = sandbx.cmd_run(name, [str(project), *args])
            return rc, out.getvalue(), err.getvalue()


class TestShellMode(CliRunner, unittest.TestCase):
    """--shell must yield the very same sandbox; only the command differs."""

    def test_runs_a_login_shell_instead_of_the_agent(self):
        for name, agent in AGENTS.items():
            with self.subTest(agent=name):
                args = argv(name, shell=True)
                tail = args[args.index(agent.image) + 1:]
                self.assertEqual(tail, [*sandbx.AS_AGENT, *sandbx.SHELL_COMMAND])

    def test_sandbox_is_identical_up_to_the_image(self):
        for name, agent in AGENTS.items():
            for kw in [{}, {"relabel": False}, {"interactive": False},
                       {"runtime": "crun"}]:
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
                tail = shlex.join(["sandbx-claude", *sandbx.AS_AGENT, "/bin/bash", "-l"])
                self.assertTrue(out.strip().endswith(tail), out)
                self.assertEqual(err, "")

    def test_real_run_prints_a_hint_and_execs_the_shell(self):
        for name, agent in AGENTS.items():
            with self.subTest(agent=name):
                _, _, err = self.run_cli(name, ["--shell"], real=True)
                self.assertIn(f"run `{agent.command[0]}`", err)
                args = self.execvp.call_args.args[1]
                self.assertEqual(args[-2:], list(sandbx.SHELL_COMMAND))

    def test_normal_run_prints_profile_and_port_line(self):
        _, _, err = self.run_cli("claude", [], real=True)
        self.assertEqual(
            err, "sandbx: profile=default, ports 30010-30019 inside reach the "
                 "same ports on host 127.0.0.1\n")
        self.assertEqual(self.execvp.call_args.args[1][-1], "claude")


class TestKvmCheck(CliRunner, unittest.TestCase):
    """Without /dev/kvm, krun fails deep inside podman; say why up front."""

    def test_missing_kvm_is_a_clean_error_and_nothing_runs(self):
        with self.assertRaises(SandboxError) as ctx:
            self.run_cli("claude", [], real=True, kvm=False)
        self.assertIn("/dev/kvm", str(ctx.exception))
        self.execvp.assert_not_called()

    def test_dry_run_works_without_kvm(self):
        rc, out, _ = self.run_cli("claude", ["--dry-run"], kvm=False)
        self.assertEqual(rc, 0)
        self.assertIn("--runtime=krun", out)

    def test_crun_runs_without_kvm(self):
        self.run_cli("claude", ["--runtime", "crun"], real=True, kvm=False)
        self.assertIn("--runtime=crun", self.execvp.call_args.args[1])


def annotations(args):
    """Every --annotation value, in order."""
    return [args[i + 1] for i, tok in enumerate(args) if tok == "--annotation"]


class TestVmSize(CliRunner, unittest.TestCase):
    """--ram and --cpus reach crun as krun annotations, and only in a range crun
    honours: out of it, crun swaps in its own default or libkrun refuses."""

    def test_defaults_set_ram_and_leave_cpus_to_crun(self):
        self.assertEqual(annotations(argv()), ["krun.ram_mib=4096"])

    def test_ram_and_cpus_pass_through(self):
        self.assertEqual(annotations(argv(ram_mib=8192, cpus=4)),
                         ["krun.ram_mib=8192", "krun.cpus=4"])

    def test_ram_crun_would_ignore_is_rejected(self):
        for ram in (0, 64, sandbx.VM_MIN_RAM_MIB):
            with self.subTest(ram=ram), self.assertRaises(SandboxError):
                argv(ram_mib=ram)
        self.assertIn("krun.ram_mib=129",
                      annotations(argv(ram_mib=sandbx.VM_MIN_RAM_MIB + 1)))

    def test_cpus_outside_libkrun_range_are_rejected(self):
        for cpus in (0, -1, sandbx.VM_MAX_CPUS + 1):
            with self.subTest(cpus=cpus), self.assertRaises(SandboxError):
                argv(cpus=cpus)
        for cpus in (1, sandbx.VM_MAX_CPUS):
            with self.subTest(cpus=cpus):
                self.assertIn(f"krun.cpus={cpus}", annotations(argv(cpus=cpus)))

    def test_parse_ram_units(self):
        cases = {"4096": 4096, "512M": 512, "512m": 512, "8G": 8192, "8g": 8192}
        for raw, mib in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(sandbx.parse_ram(raw), mib)

    def test_parse_ram_rejects_zero(self):
        for raw in ("0", "0M", "0G"):
            with self.subTest(raw=raw), self.assertRaises(
                    sandbx.argparse.ArgumentTypeError):
                sandbx.parse_ram(raw)

    def test_parse_ram_rejects_other_forms(self):
        for raw in ("", "8T", "1.5G", "-1", "G", "8 G", "8GB"):
            with self.subTest(raw=raw), self.assertRaises(
                    sandbx.argparse.ArgumentTypeError):
                sandbx.parse_ram(raw)

    def test_cli_flags_reach_the_command(self):
        rc, out, _ = self.run_cli("claude", ["--ram", "8G", "--cpus", "2", "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertEqual(annotations(shlex.split(out)),
                         ["krun.ram_mib=8192", "krun.cpus=2"])

    def test_cli_rejects_a_bad_size_or_count(self):
        with self.assertRaises(SystemExit):
            self.run_cli("claude", ["--ram", "lots", "--dry-run"])
        with self.assertRaises(SystemExit):
            self.run_cli("claude", ["--cpus", "two", "--dry-run"])
        with self.assertRaises(SandboxError):
            self.run_cli("claude", ["--cpus", "32", "--dry-run"])

    def test_cli_default_ram_comes_from_the_builder(self):
        _, out, _ = self.run_cli("claude", ["--dry-run"])
        self.assertEqual(annotations(shlex.split(out)), ["krun.ram_mib=4096"])


def without_runtime(args):
    """The argv minus everything that is allowed to differ between runtimes:
    the runtime itself, the VM annotations, and how the workload becomes agent."""
    args, n = list(args), len(sandbx.AS_AGENT)
    for i in range(len(args) - n + 1):
        if args[i:i + n] == list(sandbx.AS_AGENT):
            del args[i:i + n]
            break
    out, i = [], 0
    while i < len(args):
        if args[i] in ("--annotation", "--user"):
            i += 2
            continue
        if args[i].startswith("--runtime=") or args[i].startswith("SANDBX_RUNTIME="):
            out.append("<runtime>")
        else:
            out.append(args[i])
        i += 1
    return out


class TestRuntime(CliRunner, unittest.TestCase):
    """crun swaps the microVM for a plain container. Everything else about the
    sandbox stays put, and VM-only options are refused rather than dropped."""

    def test_krun_is_the_default(self):
        self.assertIn("--runtime=krun", argv())
        self.assertIn("SANDBX_RUNTIME=krun", argv())

    def test_crun_is_passed_explicitly(self):
        """Explicit, so a podman that defaults to runc still gets crun."""
        args = argv(runtime="crun")
        self.assertIn("--runtime=crun", args)
        self.assertIn("SANDBX_RUNTIME=crun", args)

    def test_crun_gets_no_vm_annotations(self):
        self.assertEqual(annotations(argv(runtime="crun")), [])

    def test_crun_starts_as_agent_directly(self):
        """No VMM needs root there, so there is no root start to drop from."""
        for name, agent in AGENTS.items():
            for shell, command in ((False, agent.command),
                                   (True, sandbx.SHELL_COMMAND)):
                with self.subTest(agent=name, shell=shell):
                    args = argv(name, runtime="crun", shell=shell)
                    self.assertEqual(args[args.index("--user") + 1], "1000:1000")
                    tail = args[args.index(agent.image) + 1:]
                    self.assertEqual(tail, list(command))

    def test_same_sandbox_under_either_runtime(self):
        for kw in [{}, {"shell": True}, {"relabel": False, "interactive": False}]:
            for name in AGENTS:
                with self.subTest(agent=name, opts=kw):
                    self.assertEqual(without_runtime(argv(name, runtime="crun", **kw)),
                                     without_runtime(argv(name, **kw)))

    def test_builder_rejects_an_unknown_runtime(self):
        for bad in ("runc", "", "KRUN"):
            with self.subTest(runtime=bad), self.assertRaises(SandboxError):
                argv(runtime=bad)

    def test_builder_rejects_vm_sizing_under_crun(self):
        for kw in ({"ram_mib": 8192}, {"cpus": 2}, {"ram_mib": 8192, "cpus": 2}):
            with self.subTest(opts=kw), self.assertRaises(SandboxError):
                argv(runtime="crun", **kw)

    def test_cli_selects_crun(self):
        rc, out, _ = self.run_cli("claude", ["--runtime", "crun", "--dry-run"])
        self.assertEqual(rc, 0)
        args = shlex.split(out)
        self.assertIn("--runtime=crun", args)
        self.assertEqual(annotations(args), [])
        self.assertEqual(args[-2:], ["sandbx-claude", "claude"])

    def test_cli_refuses_vm_options_under_crun(self):
        for flags in (["--ram", "8G"], ["--cpus", "2"], ["--ram", "4G", "--cpus", "1"]):
            with self.subTest(flags=flags):
                with self.assertRaises(SystemExit) as ctx:
                    self.run_cli("claude", ["--runtime", "crun", *flags, "--dry-run"])
                self.assertEqual(ctx.exception.code, 2)

    def test_cli_rejects_an_unknown_runtime(self):
        with self.assertRaises(SystemExit):
            self.run_cli("claude", ["--runtime", "runc", "--dry-run"])


class TestPorts(unittest.TestCase):
    """The reserved ranges. An overlap would loop the forwards into each other;
    a shared inbound block would stop a second sandbox from starting."""

    def ports(self, span):
        return set(range(span[0], span[1] + 1))

    def network(self, **kw):
        return next(t for t in argv(**kw) if t.startswith("--network="))

    def test_blocks_are_disjoint_from_outbound_and_each_other(self):
        seen = self.ports(sandbx.TO_HOST_PORTS)
        for block in sandbx.HOST_BLOCKS:
            with self.subTest(block=block):
                ports = self.ports(sandbx.block_ports(block))
                self.assertFalse(ports & seen)
                seen |= ports

    def test_every_block_is_block_size_wide(self):
        for block in sandbx.HOST_BLOCKS:
            with self.subTest(block=block):
                self.assertEqual(len(self.ports(sandbx.block_ports(block))),
                                 sandbx.BLOCK_SIZE)

    def test_inbound_ports_are_the_same_inside_and_on_the_host(self):
        """A remap (`host:inside`) made $SANDBX_HOST_PORTS wrong inside the
        sandbox for every block but the first."""
        for block in sandbx.HOST_BLOCKS:
            with self.subTest(block=block):
                args = argv(host_block=block)
                span = sandbx.port_span(sandbx.block_ports(block))
                inbound = self.network(host_block=block).split(",-t,", 1)[1]
                self.assertEqual(inbound, f"127.0.0.1/{span}")
                self.assertIn(f"SANDBX_HOST_PORTS={span}", args)

    def test_adb_port_is_forwarded_to_the_host(self):
        self.assertIn(sandbx.ADB_SERVER_PORT, self.ports(sandbx.TO_HOST_PORTS))

    def test_inbound_listens_on_host_loopback_only(self):
        """Without the address, pasta would expose the agent's servers to the LAN."""
        for block in sandbx.HOST_BLOCKS:
            with self.subTest(block=block):
                inbound = self.network(host_block=block).split(",-t,", 1)[1]
                self.assertTrue(inbound.startswith("127.0.0.1/"), inbound)

    def test_podman_comma_split_yields_the_intended_options(self):
        """Podman splits pasta options on commas; a comma inside a spec would
        silently turn one forward into several unrelated arguments."""
        opts = self.network().split(":", 1)[1].split(",")
        self.assertEqual(opts[0::2], ["-T", "-t"])
        self.assertEqual(len(opts), 4)

    def test_picks_the_first_free_block(self):
        self.assertEqual(sandbx.pick_host_block(lambda port: True), 30010)

    def test_skips_a_partly_taken_block(self):
        self.assertEqual(sandbx.pick_host_block(lambda port: port != 30015), 30020)

    def test_all_blocks_taken_is_a_clean_error(self):
        with self.assertRaises(SandboxError) as ctx:
            sandbx.pick_host_block(lambda port: False)
        self.assertIn("30010-30099", str(ctx.exception))

    def test_builder_rejects_an_unreserved_block(self):
        for bad in (30000, 30015, 30100, 8080):
            with self.subTest(block=bad):
                with self.assertRaises(SandboxError):
                    argv(host_block=bad)

    def test_port_free_sees_a_live_listener(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen()
            self.assertFalse(sandbx._port_free(sock.getsockname()[1]))


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

    def test_plain_build_always_skips_cache_for_agents(self):
        for tag, no_cache, pull in self.builds([]):
            with self.subTest(image=tag):
                self.assertTrue(no_cache)
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


# ---------------------------------------------------------------------------
# Profile tests
# ---------------------------------------------------------------------------

class TestProfileValidation(unittest.TestCase):
    def test_accepts_valid_names(self):
        for name in ("default", "work", "my-profile", "profile.1", "A", "a0_x"):
            with self.subTest(name=name):
                sandbx.validate_profile_name(name)  # must not raise

    def test_rejects_invalid_names(self):
        for name in ("", "/bad", "../escape", ".hidden", "a" * 65, "a/b",
                      "-leading", "_leading", ".dot"):
            with self.subTest(name=name):
                with self.assertRaises(SandboxError):
                    sandbx.validate_profile_name(name)


class ProfileTmpDir:
    """Mixin providing a temp directory tree for profile tests."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.data_home = self.tmp / "data"
        self.data_home.mkdir()
        self.env = {"XDG_DATA_HOME": str(self.data_home)}
        self.agents_root = sandbx.state_root(self.env)

    def profiles_json(self):
        return sandbx.profiles_path(self.env)


class TestProfilesJson(ProfileTmpDir, unittest.TestCase):
    def test_absent_file_returns_default(self):
        data = sandbx.load_profiles(self.env)
        self.assertEqual(data, {"profiles": ["default"], "active": "default",
                                "agent_active": {}})

    def test_roundtrip(self):
        data = {"profiles": ["default", "work"], "active": "work",
                "agent_active": {}}
        sandbx.save_profiles(data, self.env)
        self.assertEqual(sandbx.load_profiles(self.env), data)

    def test_atomic_write(self):
        sandbx.save_profiles({"profiles": ["default"], "active": "default"}, self.env)
        self.assertTrue(self.profiles_json().exists())
        self.assertFalse(self.profiles_json().with_suffix(".tmp").exists())

    def test_rejects_malformed_json(self):
        self.profiles_json().parent.mkdir(parents=True, exist_ok=True)
        self.profiles_json().write_text("{bad json")
        with self.assertRaises(SandboxError):
            sandbx.load_profiles(self.env)

    def test_rejects_empty_profiles_list(self):
        sandbx.save_profiles({"profiles": ["x"], "active": "x"}, self.env)
        self.profiles_json().write_text('{"profiles": [], "active": "x"}')
        with self.assertRaises(SandboxError):
            sandbx.load_profiles(self.env)

    def test_rejects_active_not_in_list(self):
        sandbx.save_profiles({"profiles": ["x"], "active": "x"}, self.env)
        self.profiles_json().write_text('{"profiles": ["a"], "active": "b"}')
        with self.assertRaises(SandboxError):
            sandbx.load_profiles(self.env)

    def test_active_profile_shorthand(self):
        sandbx.save_profiles({"profiles": ["default", "work"], "active": "work"},
                             self.env)
        self.assertEqual(sandbx.active_profile(env=self.env), "work")

    def test_per_agent_active_overrides_global(self):
        sandbx.save_profiles(
            {"profiles": ["default", "work", "private"], "active": "default",
             "agent_active": {"claude": "private", "copilot": "work"}},
            self.env)
        self.assertEqual(sandbx.active_profile(agent="claude", env=self.env),
                         "private")
        self.assertEqual(sandbx.active_profile(agent="copilot", env=self.env),
                         "work")
        self.assertEqual(sandbx.active_profile(agent="codex", env=self.env),
                         "default")
        self.assertEqual(sandbx.active_profile(env=self.env), "default")

    def test_rejects_agent_active_pointing_to_unknown_profile(self):
        sandbx.save_profiles(
            {"profiles": ["default"], "active": "default",
             "agent_active": {"claude": "default"}}, self.env)
        self.profiles_json().write_text(json.dumps(
            {"profiles": ["default"], "active": "default",
             "agent_active": {"claude": "gone"}}))
        with self.assertRaises(SandboxError):
            sandbx.load_profiles(self.env)



class TestProfileMount(unittest.TestCase):
    """Profile in build_podman_args."""

    def test_default_profile_mounts_default_subdir(self):
        for name, agent in AGENTS.items():
            with self.subTest(agent=name):
                config_mount = mounts(argv(name))[0]
                expected = str(AGENTS_ROOT / agent.state_dir / "default")
                self.assertTrue(config_mount.startswith(f"{expected}:"),
                                config_mount)

    def test_named_profile_mounts_named_subdir(self):
        config_mount = mounts(argv(profile="work"))[0]
        expected = str(AGENTS_ROOT / "claude" / "work")
        self.assertTrue(config_mount.startswith(f"{expected}:"), config_mount)

    def test_profile_env_var_is_set(self):
        self.assertIn("SANDBX_PROFILE=default", argv())
        self.assertIn("SANDBX_PROFILE=work", argv(profile="work"))

    def test_shape_identical_across_profiles(self):
        """The profile value is structural data, not agent-specific behavior."""
        for name in AGENTS:
            a = argv(name, profile="default")
            b = argv(name, profile="work")
            agent = AGENTS[name]
            # Replace the profile only where it appears in the mount path and
            # the SANDBX_PROFILE env var, not in unrelated tokens.
            def normalise(args, profile):
                mount_prefix = str(AGENTS_ROOT / agent.state_dir / profile)
                return [t.replace(mount_prefix, "<mount>").replace(
                    f"SANDBX_PROFILE={profile}", "SANDBX_PROFILE=<profile>")
                    for t in args]
            with self.subTest(agent=name):
                self.assertEqual(normalise(a, "default"), normalise(b, "work"))

    def test_invalid_profile_name_rejected(self):
        with self.assertRaises(SandboxError):
            argv(profile="../escape")

    def test_isolation_across_profiles(self):
        """profile=work argv must not contain the word 'default' in mount paths."""
        for name in AGENTS:
            with self.subTest(agent=name):
                config_mount = mounts(argv(name, profile="work"))[0]
                self.assertNotIn("/default", config_mount)


class TestProfileCli(CliRunner, unittest.TestCase):
    def test_dry_run_with_profile(self):
        rc, out, _ = self.run_cli("claude", ["--profile", "work", "--dry-run"])
        self.assertEqual(rc, 0)
        args = shlex.split(out)
        self.assertIn("SANDBX_PROFILE=work", args)
        config_mount = mounts(args)[0]
        self.assertIn("/work:", config_mount)

    def test_default_profile_when_omitted(self):
        rc, out, _ = self.run_cli("claude", ["--dry-run"])
        self.assertEqual(rc, 0)
        self.assertIn("SANDBX_PROFILE=default", shlex.split(out))

    def test_creates_profile_subdir(self):
        self.run_cli("claude", ["--profile", "work"], real=True)
        args = self.execvp.call_args.args[1]
        config_mount = mounts(args)[0]
        self.assertIn("/work:", config_mount)


class TestProfileCommand(ProfileTmpDir, unittest.TestCase):
    """Tests for `sandbx profile` subcommands."""

    def run_profile(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with unittest.mock.patch.dict(sandbx.os.environ, self.env), \
             contextlib.redirect_stdout(out), \
             contextlib.redirect_stderr(err):
            rc = sandbx.cmd_profile(list(args))
        return rc, out.getvalue(), err.getvalue()

    def test_create_adds_to_list(self):
        sandbx.save_profiles({"profiles": ["default"], "active": "default"},
                             self.env)
        rc, out, _ = self.run_profile("create", "work")
        self.assertEqual(rc, 0)
        data = sandbx.load_profiles(self.env)
        self.assertIn("work", data["profiles"])

    def test_create_rejects_duplicate(self):
        sandbx.save_profiles({"profiles": ["default"], "active": "default"},
                             self.env)
        with self.assertRaises(SandboxError):
            self.run_profile("create", "default")

    def test_create_validates_name(self):
        sandbx.save_profiles({"profiles": ["default"], "active": "default"},
                             self.env)
        with self.assertRaises(SandboxError):
            self.run_profile("create", "../bad")

    def test_list_shows_profiles_and_active_marker(self):
        sandbx.save_profiles({"profiles": ["default", "work"], "active": "work"},
                             self.env)
        rc, out, _ = self.run_profile("list")
        self.assertEqual(rc, 0)
        self.assertIn("work (active)", out)
        self.assertIn("default", out)
        self.assertNotIn("default (active)", out)

    def test_rename_updates_registry_and_dirs(self):
        sandbx.save_profiles({"profiles": ["default", "work"], "active": "default"},
                             self.env)
        (self.agents_root / "claude" / "work").mkdir(parents=True)
        (self.agents_root / "claude" / "work" / "creds.json").write_text("{}")
        rc, _, _ = self.run_profile("rename", "work", "job")
        self.assertEqual(rc, 0)
        data = sandbx.load_profiles(self.env)
        self.assertIn("job", data["profiles"])
        self.assertNotIn("work", data["profiles"])
        self.assertTrue((self.agents_root / "claude" / "job" / "creds.json").exists())
        self.assertFalse((self.agents_root / "claude" / "work").exists())

    def test_rename_updates_active_if_needed(self):
        sandbx.save_profiles({"profiles": ["default", "work"], "active": "work"},
                             self.env)
        self.run_profile("rename", "work", "job")
        self.assertEqual(sandbx.active_profile(env=self.env), "job")

    def test_rename_rejects_nonexistent(self):
        sandbx.save_profiles({"profiles": ["default"], "active": "default"},
                             self.env)
        with self.assertRaises(SandboxError):
            self.run_profile("rename", "nope", "new")

    def test_rename_rejects_duplicate_target(self):
        sandbx.save_profiles({"profiles": ["default", "work"], "active": "default"},
                             self.env)
        with self.assertRaises(SandboxError):
            self.run_profile("rename", "work", "default")

    def test_delete_removes_state_and_registry_entry(self):
        sandbx.save_profiles({"profiles": ["default", "work"], "active": "default"},
                             self.env)
        (self.agents_root / "claude" / "work").mkdir(parents=True)
        (self.agents_root / "claude" / "work" / "creds.json").write_text("{}")
        rc, _, _ = self.run_profile("delete", "work", "-y")
        self.assertEqual(rc, 0)
        data = sandbx.load_profiles(self.env)
        self.assertNotIn("work", data["profiles"])
        self.assertFalse((self.agents_root / "claude" / "work").exists())

    def test_delete_rejects_active_profile(self):
        sandbx.save_profiles({"profiles": ["default", "work"], "active": "default"},
                             self.env)
        with self.assertRaises(SandboxError):
            self.run_profile("delete", "default", "-y")

    def test_delete_rejects_only_profile(self):
        sandbx.save_profiles({"profiles": ["default"], "active": "default"},
                             self.env)
        with self.assertRaises(SandboxError):
            self.run_profile("delete", "default", "-y")

    def test_set_changes_active(self):
        sandbx.save_profiles({"profiles": ["default", "work"], "active": "default"},
                             self.env)
        rc, _, _ = self.run_profile("set", "work")
        self.assertEqual(rc, 0)
        self.assertEqual(sandbx.active_profile(env=self.env), "work")

    def test_set_rejects_unknown_profile(self):
        sandbx.save_profiles({"profiles": ["default"], "active": "default"},
                             self.env)
        with self.assertRaises(SandboxError):
            self.run_profile("set", "nope")

    def test_set_per_agent(self):
        sandbx.save_profiles({"profiles": ["default", "work", "private"],
                              "active": "default", "agent_active": {}},
                             self.env)
        rc, out, _ = self.run_profile("set", "private", "--agent", "claude")
        self.assertEqual(rc, 0)
        self.assertIn("claude", out)
        self.assertEqual(sandbx.active_profile(agent="claude", env=self.env),
                         "private")
        self.assertEqual(sandbx.active_profile(env=self.env), "default")

    def test_set_per_agent_does_not_change_global(self):
        sandbx.save_profiles({"profiles": ["default", "work"], "active": "default",
                              "agent_active": {}}, self.env)
        self.run_profile("set", "work", "--agent", "copilot")
        self.assertEqual(sandbx.active_profile(env=self.env), "default")
        self.assertEqual(sandbx.active_profile(agent="copilot", env=self.env),
                         "work")

    def test_rename_updates_agent_active(self):
        sandbx.save_profiles(
            {"profiles": ["default", "work"], "active": "default",
             "agent_active": {"claude": "work"}}, self.env)
        self.run_profile("rename", "work", "job")
        data = sandbx.load_profiles(self.env)
        self.assertEqual(data["agent_active"]["claude"], "job")

    def test_delete_clears_agent_active(self):
        sandbx.save_profiles(
            {"profiles": ["default", "work"], "active": "default",
             "agent_active": {"claude": "work"}}, self.env)
        rc, _, _ = self.run_profile("delete", "work", "-y")
        self.assertEqual(rc, 0)
        data = sandbx.load_profiles(self.env)
        self.assertNotIn("claude", data.get("agent_active", {}))

    def test_list_shows_agent_overrides(self):
        sandbx.save_profiles(
            {"profiles": ["default", "work"], "active": "default",
             "agent_active": {"claude": "work"}}, self.env)
        rc, out, _ = self.run_profile("list")
        self.assertEqual(rc, 0)
        self.assertIn("claude: work", out)


class TestResetWithProfiles(ProfileTmpDir, unittest.TestCase):
    def run_reset(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with unittest.mock.patch.dict(sandbx.os.environ, self.env), \
             contextlib.redirect_stdout(out), \
             contextlib.redirect_stderr(err):
            rc = sandbx.cmd_reset(list(args))
        return rc, out.getvalue(), err.getvalue()

    def test_resets_active_profile_by_default(self):
        sandbx.save_profiles({"profiles": ["default"], "active": "default"},
                             self.env)
        d = self.agents_root / "claude" / "default"
        d.mkdir(parents=True)
        (d / "creds.json").write_text("{}")
        rc, out, _ = self.run_reset("claude", "-y")
        self.assertEqual(rc, 0)
        self.assertFalse(d.exists())

    def test_resets_named_profile(self):
        sandbx.save_profiles({"profiles": ["default", "work"], "active": "default"},
                             self.env)
        d = self.agents_root / "claude" / "work"
        d.mkdir(parents=True)
        (d / "creds.json").write_text("{}")
        rc, _, _ = self.run_reset("claude", "--profile", "work", "-y")
        self.assertEqual(rc, 0)
        self.assertFalse(d.exists())
        # default profile is untouched
        default = self.agents_root / "claude" / "default"
        default.mkdir(parents=True, exist_ok=True)
        self.assertTrue(default.exists())

    def test_all_profiles_wipes_entire_agent_dir(self):
        sandbx.save_profiles({"profiles": ["default", "work"], "active": "default"},
                             self.env)
        for p in ("default", "work"):
            d = self.agents_root / "claude" / p
            d.mkdir(parents=True)
            (d / "creds.json").write_text("{}")
        rc, _, _ = self.run_reset("claude", "--all-profiles", "-y")
        self.assertEqual(rc, 0)
        self.assertFalse((self.agents_root / "claude").exists())

    def test_profile_and_all_profiles_conflict(self):
        with self.assertRaises(SystemExit):
            self.run_reset("claude", "--profile", "x", "--all-profiles", "-y")

    def test_nothing_to_reset(self):
        sandbx.save_profiles({"profiles": ["default"], "active": "default"},
                             self.env)
        self.agents_root.mkdir(parents=True, exist_ok=True)
        rc, out, _ = self.run_reset("claude", "-y")
        self.assertEqual(rc, 0)
        self.assertIn("nothing to reset", out)


class TestAssertPathWithin(unittest.TestCase):
    """Tests for the shared path-depth guard."""

    def test_accepts_direct_child(self):
        root = Path("/data/sandbx/agents")
        sandbx.assert_path_within(root / "claude", root, root)

    def test_accepts_grandchild_with_matching_parent(self):
        root = Path("/data/sandbx/agents")
        parent = root / "claude"
        sandbx.assert_path_within(parent / "default", parent, root)

    def test_rejects_wrong_depth(self):
        root = Path("/data/sandbx/agents")
        with self.assertRaises(SandboxError):
            sandbx.assert_path_within(root / "claude" / "default", root, root)

    def test_rejects_sibling(self):
        root = Path("/data/sandbx/agents")
        with self.assertRaises(SandboxError):
            sandbx.assert_path_within(Path("/data/sandbx/other"), root, root)

    def test_rejects_symlink_escape(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "agents"
            root.mkdir()
            escape = Path(d) / "escape"
            escape.mkdir()
            link = root / "trick"
            link.symlink_to(escape)
            with self.assertRaises(SandboxError):
                sandbx.assert_path_within(link, root, root)


class TestRenameValidatesOldName(ProfileTmpDir, unittest.TestCase):
    """_profile_rename must validate the old name to prevent path traversal."""

    def run_profile(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with unittest.mock.patch.dict(sandbx.os.environ, self.env), \
             contextlib.redirect_stdout(out), \
             contextlib.redirect_stderr(err):
            rc = sandbx.cmd_profile(list(args))
        return rc, out.getvalue(), err.getvalue()

    def test_rejects_traversal_in_old_name(self):
        sandbx.save_profiles(
            {"profiles": ["default", "../escape"], "active": "default"},
            self.env)
        with self.assertRaises(SandboxError):
            self.run_profile("rename", "../escape", "safe")

    def test_rename_to_invalid_name_rejected(self):
        sandbx.save_profiles(
            {"profiles": ["default", "work"], "active": "default"}, self.env)
        with self.assertRaises(SandboxError):
            self.run_profile("rename", "work", "../escape")


class TestRenameAtomicity(ProfileTmpDir, unittest.TestCase):
    """_profile_rename must not leave partial renames on failure."""

    def run_profile(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with unittest.mock.patch.dict(sandbx.os.environ, self.env), \
             contextlib.redirect_stdout(out), \
             contextlib.redirect_stderr(err):
            rc = sandbx.cmd_profile(list(args))
        return rc, out.getvalue(), err.getvalue()

    def test_no_dirs_moved_when_one_destination_exists(self):
        sandbx.save_profiles(
            {"profiles": ["default", "work"], "active": "default"}, self.env)
        agents = list(AGENTS.values())
        # Create source dirs for the first two agents
        for agent in agents[:2]:
            (self.agents_root / agent.state_dir / "work").mkdir(parents=True)
        # Pre-create a destination that will block the rename for the second agent
        (self.agents_root / agents[1].state_dir / "job").mkdir(parents=True)
        with self.assertRaises(SandboxError):
            self.run_profile("rename", "work", "job")
        # First agent's source must NOT have been moved
        self.assertTrue(
            (self.agents_root / agents[0].state_dir / "work").exists(),
            "source dir was moved despite later failure")


class TestExecvpError(unittest.TestCase):
    """os.execvp failure should produce a SandboxError, not a traceback."""

    def test_execvp_oserror_is_wrapped(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "proj"
            project.mkdir()
            env = {"HOME": str(Path(tmp) / "home"),
                   "XDG_DATA_HOME": str(Path(tmp) / "data")}
            with unittest.mock.patch.dict(sandbx.os.environ, env), \
                 unittest.mock.patch.object(
                     sandbx, "pick_host_block",
                     return_value=sandbx.HOST_BLOCKS[0]), \
                 unittest.mock.patch.object(
                     sandbx, "have_kvm", return_value=True), \
                 unittest.mock.patch.object(
                     sandbx.shutil, "which", return_value="/usr/bin/podman"), \
                 unittest.mock.patch.object(
                     sandbx.os, "execvp",
                     side_effect=OSError("permission denied")), \
                 contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SandboxError) as ctx:
                    sandbx.cmd_run("claude", [str(project)])
                self.assertIn("permission denied", str(ctx.exception))


class TestResolveProject(unittest.TestCase):
    def test_nonexistent_raises(self):
        with self.assertRaises(SandboxError):
            sandbx.resolve_project("/no/such/path/xyzzy")

    def test_file_not_dir_raises(self):
        with tempfile.NamedTemporaryFile() as f:
            with self.assertRaises(SandboxError):
                sandbx.resolve_project(f.name)

    def test_valid_dir_returns_resolved_path(self):
        with tempfile.TemporaryDirectory() as d:
            result = sandbx.resolve_project(d)
            self.assertTrue(result.is_absolute())
            self.assertEqual(result, Path(d).resolve())

    def test_symlink_is_resolved(self):
        with tempfile.TemporaryDirectory() as d:
            real = Path(d) / "real"
            real.mkdir()
            link = Path(d) / "link"
            link.symlink_to(real)
            self.assertEqual(sandbx.resolve_project(str(link)), real.resolve())


class TestSplitPassthrough(unittest.TestCase):
    def test_no_separator(self):
        self.assertEqual(sandbx.split_passthrough(["a", "b"]), (["a", "b"], []))

    def test_one_separator(self):
        self.assertEqual(
            sandbx.split_passthrough(["a", "--", "b", "c"]),
            (["a"], ["b", "c"]))

    def test_multiple_separators(self):
        before, after = sandbx.split_passthrough(["a", "--", "b", "--", "c"])
        self.assertEqual(before, ["a"])
        self.assertEqual(after, ["b", "--", "c"])

    def test_empty(self):
        self.assertEqual(sandbx.split_passthrough([]), ([], []))

    def test_only_separator(self):
        self.assertEqual(sandbx.split_passthrough(["--"]), ([], []))


class TestList(ProfileTmpDir, unittest.TestCase):
    """cmd_list was previously untested."""

    def test_shows_agent_names_and_active_profile(self):
        sandbx.save_profiles(
            {"profiles": ["default", "work"], "active": "work"}, self.env)
        for agent in AGENTS.values():
            (self.agents_root / agent.state_dir / "default").mkdir(parents=True)
            (self.agents_root / agent.state_dir / "work").mkdir(parents=True)
        out, err = io.StringIO(), io.StringIO()
        with unittest.mock.patch.dict(sandbx.os.environ, self.env), \
             unittest.mock.patch("sandbx.image_exists", return_value=False), \
             contextlib.redirect_stdout(out), \
             contextlib.redirect_stderr(err):
            rc = sandbx.cmd_list([])
        self.assertEqual(rc, 0)
        output = out.getvalue()
        self.assertIn("active profile: work", output)
        for name in AGENTS:
            self.assertIn(name, output)

    def test_shows_no_state_for_empty_agents(self):
        sandbx.save_profiles(
            {"profiles": ["default"], "active": "default"}, self.env)
        self.agents_root.mkdir(parents=True, exist_ok=True)
        out = io.StringIO()
        with unittest.mock.patch.dict(sandbx.os.environ, self.env), \
             unittest.mock.patch("sandbx.image_exists", return_value=False), \
             contextlib.redirect_stdout(out):
            sandbx.cmd_list([])
        self.assertIn("(no state)", out.getvalue())


class TestProfileBoundary(ProfileTmpDir, unittest.TestCase):
    """Profile boundary cases from the review."""

    def run_profile(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with unittest.mock.patch.dict(sandbx.os.environ, self.env), \
             contextlib.redirect_stdout(out), \
             contextlib.redirect_stderr(err):
            rc = sandbx.cmd_profile(list(args))
        return rc, out.getvalue(), err.getvalue()

    def test_boundary_length_64_accepted(self):
        sandbx.validate_profile_name("a" * 64)

    def test_boundary_length_65_rejected(self):
        with self.assertRaises(SandboxError):
            sandbx.validate_profile_name("a" * 65)

    def test_create_without_existing_profiles_json(self):
        """First-ever create works from the implicit default."""
        rc, _, _ = self.run_profile("create", "work")
        self.assertEqual(rc, 0)
        data = sandbx.load_profiles(self.env)
        self.assertIn("work", data["profiles"])
        self.assertIn("default", data["profiles"])

    def test_reset_uses_per_agent_active(self):
        """reset claude should use agent-specific active, not global."""
        sandbx.save_profiles(
            {"profiles": ["default", "work"], "active": "default",
             "agent_active": {"claude": "work"}}, self.env)
        d = self.agents_root / "claude" / "work"
        d.mkdir(parents=True)
        (d / "creds.json").write_text("{}")
        default_d = self.agents_root / "claude" / "default"
        default_d.mkdir(parents=True)
        (default_d / "keep.json").write_text("{}")
        out = io.StringIO()
        with unittest.mock.patch.dict(sandbx.os.environ, self.env), \
             contextlib.redirect_stdout(out):
            rc = sandbx.cmd_reset(["claude", "-y"])
        self.assertEqual(rc, 0)
        self.assertFalse(d.exists(), "work profile should be deleted")
        self.assertTrue(default_d.exists(), "default profile should be untouched")


if __name__ == "__main__":
    unittest.main()
