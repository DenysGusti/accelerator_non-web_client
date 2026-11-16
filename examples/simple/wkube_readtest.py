"""
Parallel read test for a file on the hf-mount (/mnt/wdrv).

Starts one WKube job per entry in READERS_PER_JOB; each job starts that many processes
that read the same file in 4 KB chunks (like the MSVC runtime under Wine) and
compare the bytes read and the checksum against the file size on disk.

A child exits with code 2 if any read was short, failed, or returned different
content, so failures show up as failed jobs.
"""
from accli import WKubeTask

# ---- settings -------------------------------------------------------------
TEST_FILE = "/mnt/wdrv/Working/6_TestRun03a_ResilientLandHAfr_a_GLOini_20260908/inputPath_constant/shifters_g4m_co2_rollmean_v2.csv"  # <-- set this
# One entry per parallel WKube job = number of reader processes in that job.
#   2 jobs, one with 1 reader and one with 4:   [1, 4]
#   2 jobs with 4 readers, 22 jobs with 1:      [4, 4] + [1] * 22
#   25 jobs with 1 reader (user's case):        [1] * 25
READERS_PER_JOB = [4] * 8
ROUNDS = 3              # only the first round reads cold; later rounds hit caches
REPORT_EVERY = 30       # seconds between progress lines

# ---- inline test script (Python 3.7 compatible) ---------------------------
READ_TEST = r'''
import hashlib, os, socket, sys, time
import multiprocessing as mp

PATH = "__FILE__"
PROCS = __PROCS__
ROUNDS = __ROUNDS__
REPORT_EVERY = __REPORT__   # seconds between progress lines
HOST = socket.gethostname()

def log(msg):
    print("[%s %s] %s" % (HOST, time.strftime("%H:%M:%S"), msg), flush=True)

def last_line(tail):
    # last non-empty line among the bytes actually read (shows where reading stopped)
    lines = [l for l in tail.replace(b"\r", b"").split(b"\n") if l.strip()]
    if not lines:
        return "<no data>"
    s = lines[-1].decode("utf-8", "replace")
    return s if len(s) <= 200 else s[:200] + "..."

def reader(idx, counters, results):
    # counters[idx] is updated after every chunk so the parent can watch progress
    h, n, t0, tail, err = hashlib.sha256(), 0, time.time(), b"", None
    try:
        with open(PATH, "rb", buffering=0) as f:
            while True:
                chunk = f.read(4096)
                if not chunk:
                    break
                h.update(chunk)
                n += len(chunk)
                counters[idx] = n
                tail = (tail + chunk)[-65536:]   # keep the end of what was read
    except OSError as e:
        err = "%s: %s" % (type(e).__name__, e)
    results.put((idx, n, None if err else h.hexdigest()[:16], err,
                 time.time() - t0, last_line(tail)))

def mb(n):
    return n / 1048576.0

def run_round(r, expected):
    counters = mp.Array("q", PROCS, lock=False)
    results = mp.Queue()
    procs = [mp.Process(target=reader, args=(i, counters, results)) for i in range(PROCS)]
    for p in procs:
        p.start()
    log("round %d: started readers pids=%s" % (r, [p.pid for p in procs]))

    t0 = time.time()
    last_n = [0] * PROCS
    last_change = [t0] * PROCS
    last_report = t0
    got = {}
    while len(got) < PROCS:
        # collect finished readers
        try:
            while True:
                res = results.get(timeout=1)
                got[res[0]] = res
        except Exception:
            pass
        now = time.time()
        for i in range(PROCS):
            if counters[i] != last_n[i]:
                last_n[i], last_change[i] = counters[i], now
        # a reader that exited without reporting would otherwise be waited on forever
        for i, p in enumerate(procs):
            if i not in got and not p.is_alive() and p.exitcode is not None:
                time.sleep(1)   # give a just-sent result time to arrive
                try:
                    while True:
                        res = results.get_nowait()
                        got[res[0]] = res
                except Exception:
                    pass
                if i not in got:
                    got[i] = (i, counters[i], None, "reader process died, exitcode=%s" % p.exitcode,
                              now - t0, "<unknown>")
        if now - last_report >= REPORT_EVERY and len(got) < PROCS:
            last_report = now
            parts = []
            for i, p in enumerate(procs):
                if i in got:
                    state = "done" if got[i][3] is None else "FAILED (%s)" % got[i][3]
                else:
                    idle = now - last_change[i]
                    state = "NO DATA FOR %ds" % idle if idle >= REPORT_EVERY else "reading"
                parts.append("r%d(pid %s) %.1f/%.1fMB %.2fMB/s %s"
                             % (i, p.pid, mb(counters[i]), mb(expected),
                                mb(counters[i]) / max(now - t0, 1e-9), state))
            log("round %d +%ds: %s" % (r, now - t0, " | ".join(parts)))

    for p in procs:
        p.join(5)
    return [got[i] for i in range(PROCS)]

if __name__ == "__main__":
    expected = os.stat(PATH).st_size
    log("file=%s expected_size=%d readers=%d rounds=%d" % (PATH, expected, PROCS, ROUNDS))
    bad = 0
    for r in range(ROUNDS):
        results = run_round(r, expected)
        digests = sorted(set(d for _, _, d, _, _, _ in results if d))
        for i, n, d, err, secs, last in results:
            status = "OK" if (err is None and n == expected) else "BAD"
            if status == "BAD":
                bad += 1
            log("round %d: r%d %s got %d/%d bytes in %.1fs sha=%s err=%s"
                % (r, i, status, n, expected, secs, d, err))
            log("round %d: r%d   last line: %s" % (r, i, last))
        if len(digests) > 1:
            bad += 1
            log("round %d: BAD different contents seen: %s" % (r, digests))
    log("RESULT: %s" % ("all reads complete" if bad == 0 else "%d bad reads" % bad))
    sys.exit(2 if bad else 0)
'''

def make_command(readers):
    script = (READ_TEST
              .replace("__FILE__", TEST_FILE)
              .replace("__PROCS__", str(readers))
              .replace("__ROUNDS__", str(ROUNDS))
              .replace("__REPORT__", str(REPORT_EVERY)))
    # The agent runs the command with `sh -c`, so a quoted heredoc passes the script verbatim.
    # -u: unbuffered output, so progress lines show up in the job log right away.
    return "python -u - <<'PYEOF'\n" + script + "\nPYEOF\n"

# ---- workflow: holder task with parallel children -------------------------
routine = WKubeTask(name="hf-mount parallel read test")

for i, readers in enumerate(READERS_PER_JOB):
    routine.add_child(WKubeTask(
        name=f"read test {i} ({readers} readers)",
        job_folder='./',
        base_stack='PYTHON3_7',
        command=make_command(readers),
        required_cores=0.5,
        required_ram=256 * 1024 ** 2,
        required_storage_local=1 * 1024 ** 3,
        required_storage_workflow=0,
        timeout=60 * 60 * 60,
        conf={
            "input_mappings": "",
            "output_mappings": ""
        }
    ))
