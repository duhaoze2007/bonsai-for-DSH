#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""spawn.py <logfile> <cmd...> —— 在新会话（setsid）里后台启动命令并打印 pid。

用 os.setsid() 脱离当前进程组，这样 harness 回收命令进程组时不会连带杀掉它。
"""

import os
import sys

if len(sys.argv) < 3:
    sys.stderr.write("usage: spawn.py <logfile> <cmd...>\n")
    sys.exit(2)

logfile = sys.argv[1]
cmd = sys.argv[2:]

pid = os.fork()
if pid:
    print(pid)
    sys.exit(0)

os.setsid()

fd = os.open(logfile, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
os.dup2(fd, 1)
os.dup2(fd, 2)
os.dup2(os.open(os.devnull, os.O_RDONLY), 0)

try:
    os.execvp(cmd[0], cmd)
except Exception as exc:  # noqa: BLE001
    os.write(2, ("spawn failed: %r\n" % (exc,)).encode())
    os._exit(1)
