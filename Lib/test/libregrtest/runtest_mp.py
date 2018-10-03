import json
import os
import queue
import sys
import time
import traceback
import types
from test import support
try:
    import threading
except ImportError:
    print("Multiprocess option requires thread support")
    sys.exit(2)

from test.libregrtest.runtest import runtest, INTERRUPTED, CHILD_ERROR
from test.libregrtest.setup import setup_tests


# Minimum duration of a test to display its duration or to mention that
# the test is running in background
PROGRESS_MIN_TIME = 30.0   # seconds

# Display the running tests if nothing happened last N seconds
PROGRESS_UPDATE = 30.0   # seconds


def run_test_in_subprocess(testname, ns):
    """Run the given test in a subprocess with --slaveargs.

    ns is the option Namespace parsed from command-line arguments. regrtest
    is invoked in a subprocess with the --slaveargs argument; when the
    subprocess exits, its return code, stdout and stderr are returned as a
    3-tuple.
    """
    from subprocess import Popen, PIPE

    ns_dict = vars(ns)
    slaveargs = (ns_dict, testname)
    slaveargs = json.dumps(slaveargs)

    cmd = [sys.executable, *support.args_from_interpreter_flags(),
           '-X', 'faulthandler',
           '-m', 'test.regrtest',
           '--slaveargs', slaveargs]
    if ns.pgo:
        cmd += ['--pgo']

    # Running the child from the same working directory as regrtest's original
    # invocation ensures that TEMPDIR for the child is the same when
    # sysconfig.is_python_build() is true. See issue 15300.
    popen = Popen(cmd,
                  stdout=PIPE, stderr=PIPE,
                  universal_newlines=True,
                  close_fds=(os.name != 'nt'),
                  cwd=support.SAVEDCWD)
    with popen:
        stdout, stderr = popen.communicate()
        retcode = popen.wait()
    return retcode, stdout, stderr


def run_tests_slave(slaveargs):
    ns_dict, testname = json.loads(slaveargs)
    ns = types.SimpleNamespace(**ns_dict)

    setup_tests(ns)

    try:
        result = runtest(ns, testname)
    except KeyboardInterrupt:
        result = INTERRUPTED, ''
    except BaseException as e:
        traceback.print_exc()
        result = CHILD_ERROR, str(e)

    print()   # Force a newline (just in case)
    print(json.dumps(result), flush=True)
    sys.exit(0)


# We do not use a generator so multiple threads can call next().
class MultiprocessIterator:

    """A thread-safe iterator over tests for multiprocess mode."""

    def __init__(self, tests):
        self.interrupted = False
        self.lock = threading.Lock()
        self.tests = tests

    def __iter__(self):
        return self

    def __next__(self):
        with self.lock:
            if self.interrupted:
                raise StopIteration('tests interrupted')
            return next(self.tests)


class MultiprocessThread(threading.Thread):
    def __init__(self, pending, output, ns):
        super().__init__()
        self.pending = pending
        self.output = output
        self.ns = ns
        self.current_test = None
        self.start_time = None

    def _runtest(self):
        try:
            test = next(self.pending)
        except StopIteration:
            self.output.put((None, None, None, None))
            return True

        try:
            self.start_time = time.monotonic()
            self.current_test = test

            retcode, stdout, stderr = run_test_in_subprocess(test, self.ns)
        finally:
            self.current_test = None

        stdout, _, result = stdout.strip().rpartition("\n")
        if retcode != 0:
            result = (CHILD_ERROR, "Exit code %s" % retcode)
            self.output.put((test, stdout.rstrip(), stderr.rstrip(),
                             result))
            return True

        if not result:
            self.output.put((None, None, None, None))
            return True

        result = json.loads(result)
        self.output.put((test, stdout.rstrip(), stderr.rstrip(),
                         result))
        return False

    def run(self):
        try:
            stop = False
            while not stop:
                stop = self._runtest()
        except BaseException:
            self.output.put((None, None, None, None))
            raise


def run_tests_multiprocess(regrtest):
    output = queue.Queue()
    pending = MultiprocessIterator(regrtest.tests)

    workers = [MultiprocessThread(pending, output, regrtest.ns)
               for i in range(regrtest.ns.use_mp)]
    for worker in workers:
        worker.start()

    def get_running(workers):
        running = []
        for worker in workers:
            current_test = worker.current_test
            if not current_test:
                continue
            dt = time.monotonic() - worker.start_time
            if dt >= PROGRESS_MIN_TIME:
                running.append('%s (%.0f sec)' % (current_test, dt))
        return running

    finished = 0
    test_index = 1
    timeout = max(PROGRESS_UPDATE, PROGRESS_MIN_TIME)
    try:
        while finished < regrtest.ns.use_mp:
            try:
                item = output.get(timeout=timeout)
            except queue.Empty:
                running = get_running(workers)
                if running and not regrtest.ns.pgo:
                    print('running: %s' % ', '.join(running))
                continue

            test, stdout, stderr, result = item
            if test is None:
                finished += 1
                continue
            regrtest.accumulate_result(test, result)

            # Display progress
            text = test
            ok, test_time = result
            if (ok not in (CHILD_ERROR, INTERRUPTED)
                and test_time >= PROGRESS_MIN_TIME
                and not regrtest.ns.pgo):
                text += ' (%.0f sec)' % test_time
            running = get_running(workers)
            if running and not regrtest.ns.pgo:
                text += ' -- running: %s' % ', '.join(running)
            regrtest.display_progress(test_index, text)

            # Copy stdout and stderr from the child process
            if stdout:
                print(stdout, flush=True)
            if stderr and not regrtest.ns.pgo:
                print(stderr, file=sys.stderr, flush=True)

            if result[0] == INTERRUPTED:
                raise KeyboardInterrupt
            if result[0] == CHILD_ERROR:
                msg = "Child error on {}: {}".format(test, result[1])
                raise Exception(msg)
            test_index += 1
    except KeyboardInterrupt:
        regrtest.interrupted = True
        pending.interrupted = True
        print()

    running = [worker.current_test for worker in workers]
    running = list(filter(bool, running))
    if running:
        print("Waiting for %s" % ', '.join(running))
    for worker in workers:
        worker.join()
