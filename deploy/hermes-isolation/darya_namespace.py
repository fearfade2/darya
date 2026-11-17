"""Filesystem boundaries for Darya conversation tasks, independent of model instructions."""
import os
import re
from pathlib import Path

WORKSPACE = Path('/data/workspace/alisa-hermes/workspace')

def current_root():
    from gateway.session_context import get_session_env
    key = get_session_env('HERMES_SESSION_KEY','')
    if not key.startswith('darya:dialog:'):
        return None
    match = re.fullmatch(r'darya:dialog:(-?\d+):(\d+):(\d+)',key)
    if not match:
        raise PermissionError('Invalid conversation namespace')
    root = WORKSPACE / ('dialog-'+'-'.join(match.groups()))
    if root.is_symlink():
        raise PermissionError('Invalid conversation workspace')
    root.mkdir(parents=True,exist_ok=True)
    (root/'.tmp').mkdir(exist_ok=True)
    return root

def check_path(path, root=None):
    root = root or current_root()
    path = Path(path)
    if root is not None:
        path = path if path.is_absolute() else root/path
        if not path.resolve().is_relative_to(root.resolve()):
            raise PermissionError('Files from other conversations are unavailable')
    return path

def shell_args(root, cwd, command):
    root = root.resolve()
    args = ['/usr/bin/bwrap','--unshare-user','--unshare-pid','--unshare-ipc',
            '--unshare-uts','--die-with-parent','--new-session','--cap-drop','ALL']
    for name in ['/usr','/bin','/sbin','/lib','/lib64','/etc']:
        if Path(name).exists():
            args += ['--ro-bind',name,name]
    args += ['--proc','/proc','--dev','/dev','--bind',str(root),str(root),
             '--bind',str(root/'.tmp'),'/tmp','--chdir',str(check_path(cwd,root)),
             '--unsetenv','API_SERVER_KEY','--unsetenv','OPENAI_API_KEY','--unsetenv','ALISA_AI_KEY',
             '--setenv','HOME',str(root),'--setenv','TMPDIR','/tmp']
    cert = Path('/data/workspace/alisa-hermes/ca-bundle.pem')
    if cert.is_file():
        args += ['--ro-bind',str(cert),'/etc/ssl/certs/ca-certificates.crt',
                 '--setenv','SSL_CERT_FILE','/etc/ssl/certs/ca-certificates.crt',
                 '--setenv','REQUESTS_CA_BUNDLE','/etc/ssl/certs/ca-certificates.crt']
    return args + ['--','/bin/bash','--noprofile','--norc','-c',command]
