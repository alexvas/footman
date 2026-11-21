"""Private fault inputs below real execution/loader boundaries, never doubles.

Only generated workspace I/O, an actual pipe release, or the Docker CLI is
faulted. All installs/suites that reach execution use the delivered real code.
"""


def setup(mode, prepared):
    values = {
        "FAULT_MODE": mode,
        "PREREQUISITE_MARKER": str(prepared["tmp"] / "controls/normal-prerequisite"),
    }
    return (
        "\n".join(f"{key} = {value!r}" for key, value in values.items()) + "\n" + SOURCE
    )


SOURCE = r"""
module._bootstrap()
import _footman_acceptance as tooling
from footman.filesystem.streams import OwnedStream
fault_events = []
original_core_write = module._Core.write
original_core_mkdir = module._Core.mkdir
original_tool_write = tooling.Tooling.write
original_open, original_write = os.open, os.write
original_close = OwnedStream.close
armed_release = False
release_faulted = False
publication_control = KeyboardInterrupt("injected real leg publication interruption")

def record(kind):
    fault_events.append(kind)

def core_write(self, path, data):
    if FAULT_MODE == "combined" and str(path).endswith("/staged_bytes/workspace/host_tests/expectation_input.json"):
        value = json.loads(data)
        value["expected_override"] = True
        data = json.dumps(value).encode()
        record("invalid-input")
    return original_core_write(self, path, data)

def mkdir(self, path):
    result = original_core_mkdir(self, path)
    if str(path).endswith("/normal/workspace/host_tests"):
        if FAULT_MODE == "normal-input":
            original_core_write(self, Path(path) / "expectation_input.json", b'{"schema_version":1,"case_id":"cleanup","expected_override":false}')
            record("normal-input")
        if FAULT_MODE == "prerequisite":
            original_core_write(self, Path(PREREQUISITE_MARKER), b"Docker CLI prerequisite fault")
            record("prerequisite")
    return result

def absolute_leaf(path, dir_fd):
    path = Path(path)
    return path if path.is_absolute() else (Path(os.readlink('/proc/self/fd/' + str(dir_fd))) if dir_fd is not None else Path.cwd()) / path

def opened(path, flags, *args, dir_fd=None, **kwargs):
    full = absolute_leaf(path, dir_fd)
    if FAULT_MODE == "combined":
        if str(full).endswith('/argv/workspace/tests/host_acceptance_assertions.py'):
            record("missing-assertions")
            raise FileNotFoundError("injected missing copied assertion closure")
        if '/normal/workspace/' in str(full) and full.name == 'provenance.json':
            record("missing-proof")
            raise FileNotFoundError("injected missing persisted normal proof")
    return original_open(path, flags, *args, dir_fd=dir_fd, **kwargs)

def written(fd, data):
    if FAULT_MODE == "combined" and os.readlink('/proc/self/fd/' + str(fd)).endswith('/command_status/workspace/tests/host_acceptance_assertions.py'):
        record("mutated-assertions")
        data = bytes(data[:-1]) + b'#'  # Same length, truthful OS write count.
    return original_write(fd, data)

def tool_write(self, path, data):
    result = original_tool_write(self, path, data)
    if FAULT_MODE == "interrupt" and str(path).endswith('/normal/evidence/report.json'):
        record("publication-interruption")
        raise publication_control
    return result

def audit_fault(event, arguments):
    global armed_release
    if FAULT_MODE == "release" and event == "subprocess.Popen":
        argv = list(map(str, arguments[1]))
        if 'pytest' in argv and str(arguments[2]).endswith('/normal/workspace'):
            armed_release = True

def close(self):
    global release_faulted
    live = not self.released
    original_close(self)
    if armed_release and live and self.label == 'stdout' and not release_faulted:
        release_faulted = True
        record("pipe-release")
        raise OSError("injected pytest pipe sole-close failure after release")

module._Core.write = core_write
module._Core.mkdir = mkdir
tooling.Tooling.write = tool_write
os.open, os.write = opened, written
OwnedStream.close = close
sys.addaudithook(audit_fault)
"""
