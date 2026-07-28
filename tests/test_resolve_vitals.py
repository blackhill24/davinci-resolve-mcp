"""Offline tests for `scripts/resolve_vitals.py` (issue #153).

The sampler cannot be tested against a real Resolve — the failure it exists to
describe does not reproduce on demand — so every reading is exercised against a
synthetic `/proc` tree written into a temp dir. That is enough to pin the parts
that are actually easy to get wrong and impossible to notice: the anchored
command-line match (`pgrep -x resolve` finds nothing, `pgrep -f resolve` finds
`systemd-resolved` — #111 finding 7), and the None-vs-zero distinction that
keeps "could not read it" from reading as "it is holding nothing".
"""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_SPEC = importlib.util.spec_from_file_location(
    "resolve_vitals",
    Path(__file__).resolve().parents[1] / "scripts" / "resolve_vitals.py",
)
vitals = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(vitals)


def write_proc(root: Path, pid: int, *, cmdline: str, ppid: int = 1,
               rss_kb: int = 1024, vm_kb: int = 4096, threads: int = 8,
               fds: int = 3, fd_limit: int = 1024,
               utime: int = 100, stime: int = 50,
               environ: dict | None = None, comm: str = "GUI Thread") -> Path:
    """Write one plausible `/proc/<pid>` into `root` and return its path."""
    proc = root / str(pid)
    (proc / "fd").mkdir(parents=True)
    for index in range(fds):
        (proc / "fd" / str(index)).write_text("", encoding="utf-8")
    (proc / "cmdline").write_bytes(cmdline.replace(" ", "\0").encode("utf-8") + b"\0")
    (proc / "status").write_text(
        f"Name:\t{comm}\nPPid:\t{ppid}\nThreads:\t{threads}\n"
        f"VmSize:\t{vm_kb} kB\nVmRSS:\t{rss_kb} kB\n",
        encoding="utf-8",
    )
    (proc / "limits").write_text(
        "Limit                     Soft Limit           Hard Limit           Units\n"
        f"Max open files            {fd_limit}                 1048576              files\n",
        encoding="utf-8",
    )
    # `fields` starts at stat field 4 (pid, comm and state are written out
    # literally below), so utime/stime — stat fields 14 and 15 — are indices 10
    # and 11 here. Verified against a real `/proc/self/stat`. The comm field is
    # parenthesised and contains a space, which is exactly what a naive
    # whitespace split of the whole line gets wrong.
    fields = ["0"] * 50
    fields[10], fields[11] = str(utime), str(stime)
    (proc / "stat").write_text(f"{pid} ({comm}) S " + " ".join(fields), encoding="utf-8")
    if environ is not None:
        blob = b"".join(f"{k}={v}".encode("utf-8") + b"\0" for k, v in environ.items())
        (proc / "environ").write_bytes(blob)
    return proc


class FindResolvePidTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_matches_the_resolve_binary_and_rejects_systemd_resolved(self):
        write_proc(self.root, 100, cmdline="/opt/resolve/bin/resolve")
        write_proc(self.root, 200, cmdline="/usr/lib/systemd/systemd-resolved")
        write_proc(self.root, 300, cmdline="/usr/bin/resolvectl query example.com")
        (self.root / "self").mkdir()  # non-numeric entries must be skipped
        self.assertEqual(vitals.find_resolve_pids(str(self.root)), [100])

    def test_bare_resolve_at_the_start_of_the_command_line_matches(self):
        write_proc(self.root, 100, cmdline="resolve --nogui")
        self.assertEqual(vitals.find_resolve_pids(str(self.root)), [100])

    def test_two_overlapping_instances_are_both_returned(self):
        write_proc(self.root, 300, cmdline="/opt/resolve/bin/resolve")
        write_proc(self.root, 100, cmdline="/opt/resolve/bin/resolve")
        self.assertEqual(vitals.find_resolve_pids(str(self.root)), [100, 300])

    def test_missing_proc_root_is_empty_not_an_exception(self):
        self.assertEqual(vitals.find_resolve_pids(str(self.root / "nope")), [])


class SampleTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_reads_every_field_off_the_synthetic_proc(self):
        write_proc(self.root, 100, cmdline="/opt/resolve/bin/resolve",
                   rss_kb=2_097_152, threads=42, fds=7, fd_limit=4096,
                   utime=200, stime=100)
        reading = vitals.sample(proc_root=str(self.root), with_gpu=False)
        self.assertTrue(reading["alive"])
        self.assertEqual(reading["pid"], 100)
        self.assertEqual(reading["rss_kb"], 2_097_152)
        self.assertEqual(reading["threads"], 42)
        self.assertEqual(reading["fds"], 7)
        self.assertEqual(reading["fd_limit"], 4096)
        self.assertEqual(reading["cpu_seconds"],
                         round(300 / (os.sysconf("SC_CLK_TCK") or 100), 2))
        self.assertNotIn("gpu_mib", reading)

    def test_descendants_are_counted_and_their_fds_summed_separately(self):
        write_proc(self.root, 100, cmdline="/opt/resolve/bin/resolve", fds=5)
        write_proc(self.root, 101, cmdline="fusionscript server", ppid=100, fds=4, rss_kb=64)
        write_proc(self.root, 102, cmdline="ffmpeg", ppid=101, fds=6, rss_kb=32)
        write_proc(self.root, 999, cmdline="unrelated", ppid=1, fds=9)
        reading = vitals.sample(proc_root=str(self.root), with_gpu=False)
        self.assertEqual(reading["children"], 2, "grandchildren count too")
        self.assertEqual(reading["fds"], 5, "the parent's own fds stay its own")
        self.assertEqual(reading["child_fds"], 10)
        self.assertEqual(reading["child_rss_kb"], 96)

    def test_no_resolve_process_is_a_recorded_state_not_an_error(self):
        write_proc(self.root, 200, cmdline="/usr/lib/systemd/systemd-resolved")
        reading = vitals.sample(proc_root=str(self.root))
        self.assertEqual(reading, {"at_epoch": mock.ANY, "alive": False, "pid": None})
        self.assertIn("GONE", vitals.format_sample(reading))

    def test_a_pid_that_vanished_between_find_and_read_is_not_alive(self):
        reading = vitals.sample(pid=100, proc_root=str(self.root))
        self.assertFalse(reading["alive"])
        self.assertEqual(reading["pid"], 100)

    def test_environ_reports_an_unset_key_as_none_not_as_absent(self):
        write_proc(self.root, 100, cmdline="/opt/resolve/bin/resolve",
                   environ={"ALSA_CONFIG_PATH": "/tmp/asound.conf", "PATH": "/usr/bin"})
        reading = vitals.sample(proc_root=str(self.root), with_gpu=False, with_env=True)
        self.assertEqual(reading["environ"]["ALSA_CONFIG_PATH"], "/tmp/asound.conf")
        self.assertIn("LC_ALL", reading["environ"])
        self.assertIsNone(reading["environ"]["LC_ALL"])


class SumOrNoneTests(unittest.TestCase):
    def test_no_children_is_zero_but_unreadable_children_is_none(self):
        self.assertEqual(vitals._sum_or_none([]), 0)
        self.assertIsNone(vitals._sum_or_none([None, None]))
        self.assertEqual(vitals._sum_or_none([4, None]), 4)


class GpuTests(unittest.TestCase):
    def _run(self, stdout: str, returncode: int = 0):
        return mock.patch.object(
            vitals.subprocess, "run",
            return_value=mock.Mock(returncode=returncode, stdout=stdout))

    def test_sums_only_the_matching_pids(self):
        with self._run("100, 512\n999, 4096\n101, 64\n"):
            self.assertEqual(vitals.gpu_memory_mib([100, 101]), 576)

    def test_a_process_holding_no_gpu_memory_is_zero(self):
        with self._run("999, 4096\n"):
            self.assertEqual(vitals.gpu_memory_mib([100]), 0)

    def test_an_unusable_nvidia_smi_is_none_never_zero(self):
        with self._run("", returncode=9):
            self.assertIsNone(vitals.gpu_memory_mib([100]))
        with mock.patch.object(vitals.subprocess, "run", side_effect=OSError):
            self.assertIsNone(vitals.gpu_memory_mib([100]))
        self.assertIsNone(vitals.gpu_memory_mib([]))


class DeltaTests(unittest.TestCase):
    def test_growth_is_reported_only_for_fields_readable_at_both_ends(self):
        first = {"rss_kb": 100, "fds": 10, "gpu_mib": None}
        last = {"rss_kb": 250, "fds": 9, "gpu_mib": 512}
        self.assertEqual(vitals.delta(first, last), {"rss_kb": 150, "fds": -1})


if __name__ == "__main__":
    unittest.main()
