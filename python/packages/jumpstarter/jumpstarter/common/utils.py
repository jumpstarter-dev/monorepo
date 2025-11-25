import os
import signal
import sys
from contextlib import ExitStack, asynccontextmanager, contextmanager
from datetime import timedelta
from functools import partial
from subprocess import Popen
from typing import TYPE_CHECKING

from anyio.from_thread import BlockingPortal, start_blocking_portal

from jumpstarter.client import client_from_path
from jumpstarter.config.env import JMP_DRIVERS_ALLOW, JUMPSTARTER_HOST
from jumpstarter.exporter import Session
from jumpstarter.utils.env import env

if TYPE_CHECKING:
    from jumpstarter.driver import Driver

__all__ = ["env"]


@asynccontextmanager
async def serve_async(root_device: "Driver", portal: BlockingPortal, stack: ExitStack):
    with Session(root_device=root_device) as session:
        async with session.serve_unix_async() as path:
            # SAFETY: the root_device instance is constructed locally thus considered trusted
            async with client_from_path(path, portal, stack, allow=[], unsafe=True) as client:
                try:
                    yield client
                finally:
                    if hasattr(client, "close"):
                        client.close()


@contextmanager
def serve(root_device: "Driver"):
    with start_blocking_portal() as portal:
        with ExitStack() as stack:
            with portal.wrap_async_context_manager(serve_async(root_device, portal, stack)) as client:
                try:
                    yield client
                finally:
                    if hasattr(client, "close"):
                        client.close()


ANSI_GRAY = "\\[\\e[90m\\]"
ANSI_YELLOW = "\\[\\e[93m\\]"
ANSI_WHITE = "\\[\\e[97m\\]"
ANSI_RESET = "\\[\\e[0m\\]"
PROMPT_CWD = "\\W"


def lease_ending_handler(process: Popen, lease, remaining_time) -> None:
    """Lease ending handler to terminate a process when lease ends.

    Args:
        process: The process to terminate
        lease: The lease instance
        remaining_time: Time remaining until lease expiration
    """

    if remaining_time <= timedelta(0):
        try:
            process.send_signal(signal.SIGHUP)
        except (ProcessLookupError, OSError):
            pass  # Process already terminated


def _run_process(
    cmd: list[str],
    env: dict[str, str],
    lease=None,
) -> int:
    """Helper to run a process with an option to set a lease ending callback."""
    process = Popen(cmd, stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr, env=env)
    if lease is not None:
        lease.lease_ending_callback = partial(lease_ending_handler, process)
    return process.wait()


def launch_shell(
    host: str,
    context: str,
    allow: list[str],
    unsafe: bool,
    use_profiles: bool,
    *,
    command: tuple[str, ...] | None = None,
    lease=None,
) -> int:
    """Launch a shell with a custom prompt indicating the exporter type.

    Args:
        host: The jumpstarter host path
        context: The context of the shell (e.g. "local" or exporter name)
        allow: List of allowed drivers
        unsafe: Whether to allow drivers outside of the allow list
        use_profiles: Whether to load shell profile files
        command: Optional command to run instead of launching an interactive shell
        lease: Optional Lease object to set up lease ending callback

    Returns:
        The exit code of the shell or command process
    """

    shell = os.environ.get("SHELL", "bash")
    shell_name = os.path.basename(shell)

    common_env = os.environ | {
        JUMPSTARTER_HOST: host,
        JMP_DRIVERS_ALLOW: "UNSAFE" if unsafe else ",".join(allow),
    }

    if command:
        return _run_process(list(command), common_env, lease)

    if shell_name.endswith("bash"):
        env = common_env | {
            "PS1": f"{ANSI_GRAY}{PROMPT_CWD} {ANSI_YELLOW}⚡{ANSI_WHITE}{context} {ANSI_YELLOW}➤{ANSI_RESET} ",
        }
        cmd = [shell]
        if not use_profiles:
            cmd.extend(["--norc", "--noprofile"])
        return _run_process(cmd, env, lease)

    elif shell_name == "fish":
        fish_fn = (
            "function fish_prompt; "
            "set_color grey; "
            'printf "%s" (basename $PWD); '
            "set_color yellow; "
            'printf "⚡"; '
            "set_color white; "
            f'printf "{context}"; '
            "set_color yellow; "
            'printf "➤ "; '
            "set_color normal; "
            "end"
        )
        cmd = [shell, "--init-command", fish_fn]
        return _run_process(cmd, common_env, lease)

    elif shell_name == "zsh":
        env = common_env | {
            "PS1": f"%F{{8}}%1~ %F{{yellow}}⚡%F{{white}}{context} %F{{yellow}}➤%f ",
        }
        if "HISTFILE" not in env:
            env["HISTFILE"] = os.path.join(os.path.expanduser("~"), ".zsh_history")

        cmd = [shell]
        if not use_profiles:
            cmd.append("--no-rcs")
        cmd.extend(["-o", "inc_append_history", "-o", "share_history"])
        return _run_process(cmd, env, lease)

    else:
        return _run_process([shell], common_env, lease)
