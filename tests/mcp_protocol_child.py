"""Development-only non-attested serialized protocol peer; inherited handles borrowed."""

import json
import os
import sys
import time


def main():
    mode = sys.argv[1]
    if mode == "oversized":
        sys.stdout.buffer.write(b"x" * (1048576 + 65536))
        sys.stdout.buffer.flush()
        return
    if mode == "many-frames":
        line = (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/message",
                    "params": {"data": "x" * 512},
                }
            )
            + "\n"
        ).encode()
        sys.stdout.buffer.write(line * 10000)
        sys.stdout.buffer.flush()
        return
    if mode == "hostile-locale":
        sys.stderr.buffer.write(
            (
                "peer locale "
                + " ".join(
                    f"{key}={os.environ.get(key)}"
                    for key in (
                        "LC_ALL",
                        "LANG",
                        "PYTHONIOENCODING",
                        "PYTHONUTF8",
                        "PYTHONCOERCECLOCALE",
                    )
                )
                + f" stdin={sys.stdin.encoding} stdout={sys.stdout.encoding} utf8_mode={sys.flags.utf8_mode}\n"
            ).encode("ascii")
        )
        sys.stderr.buffer.flush()
    if mode == "eof":
        return
    if mode == "blocked":
        time.sleep(2)
        return
    if mode == "stderr":
        sys.stderr.buffer.write(b"peer diagnostic\xff\n")
        sys.stderr.buffer.flush()
    for line in sys.stdin.buffer:
        value = json.loads(line)
        if "id" not in value:
            continue
        if mode == "delay":
            time.sleep(0.1)
        if mode == "malformed":
            sys.stdout.buffer.write(b"not-json\n")
            sys.stdout.buffer.flush()
            return
        if mode == "partial":
            sys.stdout.buffer.write(b'{"jsonrpc":')
            sys.stdout.buffer.flush()
            return
        if value["method"] == "initialize":
            result = {
                "protocolVersion": value["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "ordinary-peer", "version": "1"},
            }
        elif value["method"] == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "echo",
                        "description": "UTF-8 peer",
                        "inputSchema": {"type": "object"},
                    }
                ]
            }
        else:
            result = {
                "content": [
                    {
                        "type": "text",
                        "text": value.get("params", {})
                        .get("arguments", {})
                        .get("text", "привет"),
                    }
                ],
                "isError": False,
            }
        data = (
            json.dumps(
                {"jsonrpc": "2.0", "id": value["id"], "result": result},
                ensure_ascii=False,
            )
            + "\n"
        ).encode("utf-8")
        width = 1 if mode in {"split", "utf8", "hostile-locale"} else len(data)
        for offset in range(0, len(data), width):
            sys.stdout.buffer.write(data[offset : offset + width])
            sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
