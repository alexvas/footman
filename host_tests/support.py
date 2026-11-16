"""Resource-free pytest registration for isolated host acceptance.

Host execution keeps real OS timing. No development clock fixtures or
checkout conftest inheritance are required.
"""

HOST_MARKERS = (
    "process_time: process scenario (real OS timing on the host)",
    "real_process_time: keep operating-system process timing real",
    "own_process_time: scenario supplies its own process clock",
)
