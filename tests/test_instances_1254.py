"""#1254: a second tlwu instance in CPU mode sees the first one's threads, takes only what is left and warns once
with the other instance's PID. Real processes, real OS locks."""
import os
import subprocess
import sys
import textwrap
from types import SimpleNamespace

import pytest

from tinglongwu import cli, instances

HOLDER = textwrap.dedent("""
    import sys
    from tinglongwu import instances
    assert instances.register(int(sys.argv[1]))
    print("ready", flush=True)
    sys.stdin.read()  # hold the registration until the test closes stdin
""")


@pytest.fixture
def first_instance(tmp_path):
    """Another process registered as a CPU-mode tlwu using 12 threads (the field case: asr 6 + mt 6 on 12 CPUs)."""
    procs = []

    def start(threads):
        p = subprocess.Popen([sys.executable, "-c", HOLDER, str(threads)], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, text=True, env=os.environ.copy())
        assert p.stdout.readline().strip() == "ready"
        procs.append(p)
        return p

    yield start
    for p in procs:
        if p.poll() is None:
            p.stdin.close()
            p.wait(timeout=20)


def _args():
    return SimpleNamespace(model="small", quality="auto", min_words=0, min_words_explicit=True)


def test_second_instance_shares_the_cpu_and_names_the_first_pid(first_instance, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("TLWU_CPU_THREADS", raising=False)
    other = first_instance(8)
    a = _args()
    cli.apply_cpu_mode(a, "cpu", tmp_path, explicit_model=True, cores=12, ram_gb=16)
    err = capsys.readouterr().err
    assert f"PID {other.pid} (8 threads)" in err
    from tinglongwu import threads

    mt = threads.current()["mt"]
    assert a.cpu_threads + mt == 4  # 12 CPUs - 8 taken by the other instance
    assert 8 + a.cpu_threads + mt <= 12
    assert "oversubscribed" not in err
    # and the first instance would see this one too
    assert [o.threads for o in instances.others({"TLWU_INSTANCE_DIR": os.environ["TLWU_INSTANCE_DIR"]})] == [8]


def test_full_cpu_taken_still_runs_and_says_oversubscribed(first_instance, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("TLWU_CPU_THREADS", raising=False)
    other = first_instance(12)
    a = _args()
    cli.apply_cpu_mode(a, "cpu", tmp_path, explicit_model=True, cores=12, ram_gb=16)
    err = capsys.readouterr().err
    from tinglongwu import threads

    assert a.cpu_threads + threads.current()["mt"] == instances.MIN_THREADS
    assert f"PID {other.pid}" in err and "oversubscribed" in err


def test_transcript_only_registers_asr_threads_only(tmp_path, monkeypatch):
    monkeypatch.delenv("TLWU_CPU_THREADS", raising=False)
    a = _args()
    a.transcript_only = True
    cli.apply_cpu_mode(a, "cpu", tmp_path, explicit_model=True, cores=12, ram_gb=16)
    code = "from tinglongwu import instances; print([o.threads for o in instances.others()])"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=os.environ.copy())
    assert out.stdout.strip() == f"[{a.cpu_threads}]"


def test_ended_instance_is_not_counted_and_its_files_go(first_instance, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("TLWU_CPU_THREADS", raising=False)
    other = first_instance(8)
    other.kill()  # a crash: no clean-up ran, the OS dropped the lock
    other.wait(timeout=20)
    a = _args()
    cli.apply_cpu_mode(a, "cpu", tmp_path, explicit_model=True, cores=12, ram_gb=16)
    assert "another tlwu" not in capsys.readouterr().err
    d = os.environ["TLWU_INSTANCE_DIR"]
    assert not os.path.exists(os.path.join(d, f"{other.pid}.json"))


def test_alone_no_warning_and_release_frees_the_threads(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("TLWU_CPU_THREADS", raising=False)
    a = _args()
    cli.apply_cpu_mode(a, "cpu", tmp_path, explicit_model=True, cores=12, ram_gb=16)
    assert "another tlwu" not in capsys.readouterr().err
    code = "from tinglongwu import instances; print(len(instances.others()))"
    seen = lambda: subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,  # noqa: E731
                                  env=os.environ.copy()).stdout.strip()
    assert seen() == "1"
    instances.release()
    assert seen() == "0"


def test_gpu_run_registers_nothing(tmp_path):
    a = SimpleNamespace(model="large-v3", quality="auto")
    cli.apply_cpu_mode(a, "cuda", tmp_path, explicit_model=False)
    d = os.environ["TLWU_INSTANCE_DIR"]
    assert not os.path.isdir(d) or not os.listdir(d)
