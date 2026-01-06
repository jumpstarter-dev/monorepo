import logging
import os
import signal
import sys

import anyio
import click
from anyio import create_task_group, open_signal_receiver
from jumpstarter_cli_common.config import opt_config
from jumpstarter_cli_common.exceptions import handle_exceptions

logger = logging.getLogger(__name__)


def _handle_exporter_exceptions(excgroup):
    """Handle exceptions from exporter serving."""
    from jumpstarter_cli_common.exceptions import leaf_exceptions
    for exc in leaf_exceptions(excgroup):
        if not isinstance(exc, anyio.get_cancelled_exc_class()):
            click.echo(
                f"Exception while serving on the exporter: {type(exc).__name__}: {exc}",
                err=True,
            )


def _reap_zombie_processes(capture_child=None):
    """Reap zombie processes when running as PID 1."""
    try:
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
                if pid == 0:
                    break # No more children
                if capture_child and pid == capture_child['pid']:
                    capture_child['status'] = status
                logger.debug(f"PARENT: Reaped zombie process {pid} with status {status}")
            except ChildProcessError:
                break # No more children
    except Exception as e:
        logger.warning(f"PARENT: Error during zombie reaping: {e}")


def _handle_child(config):
    """Handle child process with graceful shutdown."""
    async def serve_with_graceful_shutdown():
        received_signal = 0
        signal_handled = False
        exporter = None

        async def signal_handler():
            nonlocal received_signal, signal_handled

            with open_signal_receiver(signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT) as signals:
                async for sig in signals:
                    if signal_handled:
                        continue  # Ignore duplicate signals
                    received_signal = sig
                    logger.info("CHILD: Received %d (%s)", received_signal, signal.Signals(received_signal).name)

                    if exporter:
                        # Terminate exporter. SIGHUP waits until current lease is let go. Later SIGTERM still overrides
                        if received_signal != signal.SIGHUP:
                            signal_handled = True
                        exporter.stop(wait_for_lease_exit=received_signal == signal.SIGHUP, should_unregister=True)

                # Start signal handler first, then create exporter
        async with create_task_group() as signal_tg:

            # Start signal handler immediately
            signal_tg.start_soon(signal_handler)

            # Create exporter and run it
            async with config.create_exporter() as exporter:
                try:
                    await exporter.serve()
                except* Exception as excgroup:
                    _handle_exporter_exceptions(excgroup)

                # Check if exporter set an exit code (e.g., from hook failure with on_failure='exit')
                exporter_exit_code = exporter.exit_code

            # Cancel the signal handler after exporter completes
            signal_tg.cancel_scope.cancel()

        # Return exit code in priority order:
        # 1. Signal number if received (for signal-based termination)
        # 2. Exporter's exit code if set (for hook failure with on_failure='exit')
        # 3. 0 for immediate restart (normal exit without signal or explicit exit code)
        if received_signal:
            return received_signal
        elif exporter_exit_code is not None:
            return exporter_exit_code
        else:
            return 0

    sys.exit(anyio.run(serve_with_graceful_shutdown))


def _wait_for_child(pid, child_info):
    """Wait for child process, get status from signal handler if reaped."""
    try:
        _, status = os.waitpid(pid, 0)
    except ChildProcessError:
        status = child_info['status']
    return status


def _handle_parent(pid):
    """Handle parent process waiting for child and signal forwarding."""
    child_info = {'pid': pid, 'status': None}

    def parent_signal_handler(signum, _):
        if signum == signal.SIGCHLD and os.getpid() == 1:
            _reap_zombie_processes(capture_child=child_info) # capture our own direct child if reaped
        elif signum != signal.SIGCHLD:
            logger.info("PARENT: Got %d (%s), forwarding to child PG %d", signum, signal.Signals(signum).name, pid)
            if pid > 0:
                try:
                    os.killpg(pid, signum)
                except (ProcessLookupError, OSError):
                    pass

    # Set up signal handlers after fork
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT, signal.SIGCHLD):
        signal.signal(sig, parent_signal_handler)

    status = _wait_for_child(pid, child_info)
    if status is None:
        return None

    if os.WIFEXITED(status):
        # Interpret child exit code
        child_exit_code = os.WEXITSTATUS(status)
        if child_exit_code == 0:
            return None  # restart child (unexpected exit/exception)
        else:
            # Child indicates termination (signal number)
            return 128 + child_exit_code  # Return standard Unix exit code
    else:
        # Child killed by unhandled signal - terminate
        child_exit_signal = os.WTERMSIG(status) if os.WIFSIGNALED(status) else 0
        click.echo(f"Child killed by unhandled signal: {child_exit_signal}", err=True)
        return 128 + child_exit_signal


def _serve_with_exc_handling(config):
    while True:
        pid = os.fork()

        if pid > 0:
            if (exit_code := _handle_parent(pid)) is not None:
                return exit_code
        else:
            os.setsid() # Become group leader so all spawned subprocesses are reached by parent's signals
            _handle_child(config)
            sys.exit(1) # should never happen


@click.command("run")
@opt_config(client=False)
@handle_exceptions
def run(config):
    """Run an exporter locally."""
    return _serve_with_exc_handling(config)
