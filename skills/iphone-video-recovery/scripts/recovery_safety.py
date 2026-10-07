"""Filesystem guards shared by recovery helpers. SPDX-License-Identifier: MIT."""
import ctypes
import errno
import os
import sys
from pathlib import Path


def ensure_not_in_backup(path):
    resolved = Path(path).resolve()
    for parent in (resolved, *resolved.parents):
        if (parent / 'Manifest.db').is_file() or (parent / 'Manifest.mbdb').is_file():
            raise ValueError(f'Output or repair target is inside an original backup: {resolved}')
    return resolved


def ensure_volume(destination, volume=None):
    destination = Path(destination).absolute()
    if volume is None and sys.platform == 'darwin' and destination.parts[:2] == ('/', 'Volumes'):
        if len(destination.parts) < 3:
            raise ValueError('An external-volume destination needs a volume name')
        volume = Path(*destination.parts[:3])
    if volume is not None:
        volume = Path(volume).absolute()
        if not volume.is_dir() or not os.path.ismount(volume):
            raise ValueError(f'Destination volume is not mounted: {volume}')
        if not destination.resolve().is_relative_to(volume.resolve()):
            raise ValueError('Destination is outside the specified volume')
    return volume


def publish_no_replace(partial, destination):
    """Publish on the same filesystem; never replace an existing destination."""
    partial, destination = Path(partial), Path(destination)
    try:
        os.link(partial, destination)
    except OSError as exc:
        if exc.errno not in {errno.EPERM, errno.EOPNOTSUPP, errno.ENOTSUP, errno.EINVAL}:
            raise
        # exFAT has no hardlinks. Native exclusive rename works without duplicating
        # a large file and closes the check-then-rename overwrite race.
        if sys.platform == 'win32':
            os.rename(partial, destination)  # Windows rename refuses existing files.
            return
        libc = ctypes.CDLL(None, use_errno=True)
        if sys.platform == 'darwin':
            operation = libc.renamex_np
            operation.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
            arguments = (os.fsencode(partial), os.fsencode(destination), 4)  # RENAME_EXCL
        elif sys.platform.startswith('linux') and hasattr(libc, 'renameat2'):
            operation = libc.renameat2
            operation.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                                  ctypes.c_char_p, ctypes.c_uint]
            arguments = (-100, os.fsencode(partial), -100, os.fsencode(destination), 1)
        else:
            raise OSError('Exclusive rename is unavailable; verified partial file was retained') from exc
        operation.restype = ctypes.c_int
        if operation(*arguments) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), str(destination))
    else:
        partial.unlink()


def write_json_exclusive(path, value):
    import json
    path = Path(path)
    ensure_not_in_backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False)
        handle.write('\n')
