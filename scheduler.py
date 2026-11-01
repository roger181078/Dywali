#!/usr/bin/env python3
# Copyright (C) 2026 roger181078
# SPDX-License-Identifier: GPL-3.0-only
"""Instala un timer systemd --user. No altera programaciones antiguas."""
from pathlib import Path
from i18n import _
import subprocess
import shutil

SERVICE = 'dywali.service'
TIMER = 'dywali.timer'
DIR = Path.home()/'.config/systemd/user'
EXEC = Path.home()/'.local/bin/dywali-apply'

def systemctl(*args):
    return subprocess.run(['systemctl','--user',*args],capture_output=True,text=True)

def available():
    return shutil.which('systemctl') is not None and systemctl('show-environment').returncode == 0

def enabled():
    return systemctl('is-enabled',TIMER).returncode == 0

def install(minutes):
    minutes = int(minutes)
    if minutes not in (1,2,5,10,15,30,60):
        raise ValueError(_('Unsupported update interval.'))
    if not EXEC.is_file():
        raise RuntimeError(_('Installed engine not found: {path}').format(path=EXEC))
    if not available():
        raise RuntimeError(_('systemd --user is not available in this session.'))
    DIR.mkdir(parents=True,exist_ok=True)
    (DIR/SERVICE).write_text('[Unit]\nDescription=Actualizar fondo de Dywali\n\n[Service]\nType=oneshot\nExecStart='+str(EXEC)+'\n', encoding='utf-8')
    (DIR/TIMER).write_text('[Unit]\nDescription=Actualización periódica Dywali\n\n[Timer]\nOnBootSec=1min\nOnUnitActiveSec='+str(minutes)+'min\nAccuracySec=15s\nUnit='+SERVICE+'\n\n[Install]\nWantedBy=timers.target\n',encoding='utf-8')
    for cmd in [('daemon-reload',),('enable','--now',TIMER),('restart',TIMER)]:
        p=systemctl(*cmd)
        if p.returncode: raise RuntimeError(p.stderr.strip() or p.stdout.strip())

def disable():
    if not available(): raise RuntimeError(_('systemd --user is not available.'))
    p=systemctl('disable','--now',TIMER)
    if p.returncode and 'does not exist' not in p.stderr:
        raise RuntimeError(p.stderr.strip() or p.stdout.strip())
