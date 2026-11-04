"""A live non-EOF stdin pipe with one explicit descriptor owner."""

import os
from contextlib import contextmanager

from footman.filesystem.descriptors import OwnedDescriptor
from footman.filesystem.operations import PosixDescriptorOps

PAYLOAD = b"MCP protocol must stay unread\n" * 10


@contextmanager
def live_stdin(path):
    # Opening a FIFO read/write keeps its writer live without another fd owner.
    # NONBLOCK also makes a missing unread sentinel fail rather than hang.
    ops = PosixDescriptorOps()
    label = "live parent protocol stdin"
    flags = os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC
    os.mkfifo(path, 0o600)
    fd = os.open(path, flags)
    with OwnedDescriptor(ops, fd, label=label) as owner:
        if os.write(owner.fd, PAYLOAD) != len(PAYLOAD):
            raise OSError("could not prepare live stdin sentinel")
        yield owner
