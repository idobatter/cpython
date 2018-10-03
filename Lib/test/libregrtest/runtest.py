import faulthandler
import importlib
import io
import os
import sys
import time
import traceback
import unittest
from test import support
from test.libregrtest.refleak import dash_R
from test.libregrtest.save_env import saved_test_environment


# Test result constants.
PASSED = 1
FAILED = 0
ENV_CHANGED = -1
SKIPPED = -2
RESOURCE_DENIED = -3
INTERRUPTED = -4
CHILD_ERROR = -5   # error in a child process


# small set of tests to determine if we have a basically functioning interpreter
# (i.e. if any of these fail, then anything else is likely to follow)
STDTESTS = [
    'test_grammar',
    'test_opcodes',
    'test_dict',
    'test_builtin',
    'test_exceptions',
    'test_types',
    'test_unittest',
    'test_doctest',
    'test_doctest2',
    'test_support'
]

# set of tests that we don't want to be executed when using regrtest
NOTTESTS = set()


def findtests(testdir=None, stdtests=STDTESTS, nottests=NOTTESTS):
    """Return a list of all applicable test modules."""
    testdir = findtestdir(testdir)
    names = os.listdir(testdir)
    tests = []
    others = set(stdtests) | nottests
    for name in names:
        mod, ext = os.path.splitext(name)
        if mod[:5] == "test_" and ext in (".py", "") and mod not in others:
            tests.append(mod)
    return stdtests + sorted(tests)


def runtest(ns, test):
    """Run a single test.

    test -- the name of the test
    verbose -- if true, print more messages
    quiet -- if true, don't print 'skipped' messages (probably redundant)
    huntrleaks -- run multiple times to test for leaks; requires a debug
                  build; a triple corresponding to -R's three arguments
    output_on_failure -- if true, display test output on failure
    timeout -- dump the traceback and exit if a test takes more than
               timeout seconds
    failfast, match_tests -- See regrtest command-line flags for these.
    pgo -- if true, suppress any info irrelevant to a generating a PGO build

    Returns the tuple result, test_time, where result is one of the constants:
        INTERRUPTED      KeyboardInterrupt when run under -j
        RESOURCE_DENIED  test skipped because resource denied
        SKIPPED          test skipped for some other reason
        ENV_CHANGED      test failed because it changed the execution environment
        FAILED           test failed
        PASSED           test passed
    """

    verbose = ns.verbose
    quiet = ns.quiet
    huntrleaks = ns.huntrleaks
    output_on_failure = ns.verbose3
    failfast = ns.failfast
    match_tests = ns.match_tests
    timeout = ns.timeout
    pgo = ns.pgo

    use_timeout = (timeout is not None)
    if use_timeout:
        faulthandler.dump_traceback_later(timeout, exit=True)
    try:
        support.match_tests = match_tests
        if failfast:
            support.failfast = True
        if output_on_failure:
            support.verbose = True

            # Reuse the same instance to all calls to runtest(). Some
            # tests keep a reference to sys.stdout or sys.stderr
            # (eg. test_argparse).
            if runtest.stringio is None:
                stream = io.StringIO()
                runtest.stringio = stream
            else:
                stream = runtest.stringio
                stream.seek(0)
                stream.truncate()

            orig_stdout = sys.stdout
            orig_stderr = sys.stderr
            try:
                sys.stdout = stream
                sys.stderr = stream
                result = runtest_inner(test, verbose, quiet, huntrleaks,
                                       display_failure=False, pgo=pgo)
                if result[0] == FAILED:
                    output = stream.getvalue()
                    orig_stderr.write(output)
                    orig_stderr.flush()
            finally:
                sys.stdout = orig_stdout
                sys.stderr = orig_stderr
        else:
            support.verbose = verbose  # Tell tests to be moderately quiet
            result = runtest_inner(test, verbose, quiet, huntrleaks,
                                   display_failure=not verbose, pgo=pgo)
        return result
    finally:
        if use_timeout:
            faulthandler.cancel_dump_traceback_later()
        cleanup_test_droppings(test, verbose)
runtest.stringio = None


def runtest_inner(test, verbose, quiet,
                  huntrleaks=False, display_failure=True, *, pgo=False):
    support.unload(test)

    test_time = 0.0
    refleak = False  # True if the test leaked references.
    try:
        if test.startswith('test.'):
            abstest = test
        else:
            # Always import it from the test package
            abstest = 'test.' + test
        with saved_test_environment(test, verbose, quiet, pgo=pgo) as environment:
            start_time = time.time()
            the_module = importlib.import_module(abstest)
            # If the test has a test_main, that will run the appropriate
            # tests.  If not, use normal unittest test loading.
            test_runner = getattr(the_module, "test_main", None)
            if test_runner is None:
                def test_runner():
                    loader = unittest.TestLoader()
                    tests = loader.loadTestsFromModule(the_module)
                    for error in loader.errors:
                        print(error, file=sys.stderr)
                    if loader.errors:
                        raise Exception("errors while loading tests")
                    support.run_unittest(tests)
            test_runner()
            if huntrleaks:
                refleak = dash_R(the_module, test, test_runner, huntrleaks)
            test_time = time.time() - start_time
    except support.ResourceDenied as msg:
        if not quiet and not pgo:
            print(test, "skipped --", msg, flush=True)
        return RESOURCE_DENIED, test_time
    except unittest.SkipTest as msg:
        if not quiet and not pgo:
            print(test, "skipped --", msg, flush=True)
        return SKIPPED, test_time
    except KeyboardInterrupt:
        raise
    except support.TestFailed as msg:
        if not pgo:
            if display_failure:
                print("test", test, "failed --", msg, file=sys.stderr,
                      flush=True)
            else:
                print("test", test, "failed", file=sys.stderr, flush=True)
        return FAILED, test_time
    except:
        msg = traceback.format_exc()
        if not pgo:
            print("test", test, "crashed --", msg, file=sys.stderr,
                  flush=True)
        return FAILED, test_time
    else:
        if refleak:
            return FAILED, test_time
        if environment.changed:
            return ENV_CHANGED, test_time
        return PASSED, test_time


def cleanup_test_droppings(testname, verbose):
    import shutil
    import stat
    import gc

    # First kill any dangling references to open files etc.
    # This can also issue some ResourceWarnings which would otherwise get
    # triggered during the following test run, and possibly produce failures.
    gc.collect()

    # Try to clean up junk commonly left behind.  While tests shouldn't leave
    # any files or directories behind, when a test fails that can be tedious
    # for it to arrange.  The consequences can be especially nasty on Windows,
    # since if a test leaves a file open, it cannot be deleted by name (while
    # there's nothing we can do about that here either, we can display the
    # name of the offending test, which is a real help).
    for name in (support.TESTFN,
                 "db_home",
                ):
        if not os.path.exists(name):
            continue

        if os.path.isdir(name):
            kind, nuker = "directory", shutil.rmtree
        elif os.path.isfile(name):
            kind, nuker = "file", os.unlink
        else:
            raise SystemError("os.path says %r exists but is neither "
                              "directory nor file" % name)

        if verbose:
            print("%r left behind %s %r" % (testname, kind, name))
        try:
            # if we have chmod, fix possible permissions problems
            # that might prevent cleanup
            if (hasattr(os, 'chmod')):
                os.chmod(name, stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO)
            nuker(name)
        except Exception as msg:
            print(("%r left behind %s %r and it couldn't be "
                "removed: %s" % (testname, kind, name, msg)), file=sys.stderr)


def findtestdir(path=None):
    return path or os.path.dirname(os.path.dirname(__file__)) or os.curdir
