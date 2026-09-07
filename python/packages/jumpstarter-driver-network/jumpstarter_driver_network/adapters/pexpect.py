import socket
from contextlib import contextmanager

from pexpect.fdpexpect import fdspawn

from .portforward import TcpPortforwardAdapter
from jumpstarter.client import DriverClient


@contextmanager
def PexpectAdapter(*, client: DriverClient, method: str = "connect"):
    with TcpPortforwardAdapter(client=client, method=method) as addr:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect(addr)

        try:
            yield fdspawn(sock)
        finally:
            try:
                sock.close()
            except OSError:
                pass  # fd already closed by fdspawn
