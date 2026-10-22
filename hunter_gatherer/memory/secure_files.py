"""Open regular files without following symlinks or Windows reparse points.

Both platforms walk one component at a time relative to an open parent, never
following symlinks or Windows reparse points. Callers own the returned binary
descriptor.
"""
from __future__ import annotations

import errno
import os
import stat
from pathlib import Path


def open_regular(path: Path, *, writable: bool = False, create: bool = False) -> int:
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts or "\x00" in str(path):
        raise OSError(errno.EINVAL, "An absolute, traversal-free path is required")
    if os.name == "nt":
        return _open_windows(path, writable=writable, create=create)
    flags = os.O_RDWR if writable else os.O_RDONLY
    flags |= os.O_NOFOLLOW | os.O_NONBLOCK
    if create:
        flags |= os.O_CREAT
    parent = stream = None
    try:
        parent = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for component in path.parts[1:-1]:
            following = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=parent)
            os.close(parent)
            parent = following
        try:
            info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            if not create:
                raise
        else:
            if not stat.S_ISREG(info.st_mode):
                raise OSError(errno.ELOOP, "Not a regular file")
        stream = os.open(path.name, flags, 0o600, dir_fd=parent)
        if not stat.S_ISREG(os.fstat(stream).st_mode):
            raise OSError(errno.ELOOP, "Not a regular file")
        descriptor, stream = stream, None
        return descriptor
    finally:
        if stream is not None:
            os.close(stream)
        if parent is not None:
            os.close(parent)


def _open_windows(path: Path, *, writable: bool, create: bool) -> int:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    # Reject alternate streams, device namespaces, and Win32 aliases before IO.
    def unsafe(part):
        stem = part.split(".", 1)[0].upper()
        reserved = stem in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
        reserved |= len(stem) == 4 and stem[:3] in {"COM", "LPT"} and stem[3] in "123456789¹²³"
        return reserved or ":" in part or part.endswith((".", " "))

    if str(path).startswith(("\\\\?\\", "\\\\.\\")) or any(unsafe(part) for part in path.parts[1:]):
        raise OSError(errno.EINVAL, "Unsafe Windows path component")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.GetFileType.argtypes = [wintypes.HANDLE]
    kernel.GetFileType.restype = wintypes.DWORD

    class AttributeTag(ctypes.Structure):
        _fields_ = [("attributes", wintypes.DWORD), ("reparse_tag", wintypes.DWORD)]

    kernel.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                   wintypes.LPVOID, wintypes.DWORD]
    kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL
    native = ctypes.WinDLL("ntdll")

    class UnicodeString(ctypes.Structure):
        _fields_ = [("length", wintypes.USHORT), ("maximum_length", wintypes.USHORT),
                    ("buffer", wintypes.LPWSTR)]

    class ObjectAttributes(ctypes.Structure):
        _fields_ = [("length", wintypes.ULONG), ("root", wintypes.HANDLE),
                    ("name", ctypes.POINTER(UnicodeString)), ("attributes", wintypes.ULONG),
                    ("security", wintypes.LPVOID), ("security_qos", wintypes.LPVOID)]

    class IoStatusBlock(ctypes.Structure):
        _fields_ = [("status", wintypes.LPVOID), ("information", ctypes.c_size_t)]

    native.NtCreateFile.argtypes = [ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD,
                                   ctypes.POINTER(ObjectAttributes), ctypes.POINTER(IoStatusBlock),
                                   wintypes.LPVOID, wintypes.ULONG, wintypes.ULONG,
                                   wintypes.ULONG, wintypes.ULONG, wintypes.LPVOID, wintypes.ULONG]
    native.NtCreateFile.restype = wintypes.LONG
    native.RtlNtStatusToDosError.argtypes = [wintypes.LONG]
    native.RtlNtStatusToDosError.restype = wintypes.ULONG
    handles = []

    def opened(target, *, directory, parent=None):
        access = 0x001000A1 if directory else (0x0012019F if writable else 0x00120089)
        # Only lock-file leaves need to share writes. This is not the no-follow
        # boundary: attribute-only reparse changes can bypass sharing rules.
        sharing = 0x3 if writable and not directory else 0x1
        if parent is None:
            # Only the explicit drive/share root is opened by full path.
            handle = kernel.CreateFileW(str(target), access, sharing, None, 3,
                                        0x00200000 | 0x02000000, None)
            if handle == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
        else:
            # NtCreateFile RootDirectory is Windows' descriptor-relative open.
            # A single component plus FILE_OPEN_REPARSE_POINT cannot traverse
            # a renamed parent or a parent converted into a junction in place.
            name = str(target)
            if any(separator in name for separator in ("/", "\\", ":", "\x00")) or name in {"", ".", ".."}:
                raise OSError(errno.EINVAL, "Expected one path component")
            size = len(name.encode("utf-16-le"))
            if size > 65532:
                raise OSError(errno.ENAMETOOLONG, "Path component too long")
            buffer = ctypes.create_unicode_buffer(name)
            text = UnicodeString(size, size + 2, ctypes.cast(buffer, wintypes.LPWSTR))
            attributes = ObjectAttributes(ctypes.sizeof(ObjectAttributes), parent,
                                          ctypes.pointer(text), 0x40, None, None)
            result, status = wintypes.HANDLE(), IoStatusBlock()
            code = native.NtCreateFile(ctypes.byref(result), access, ctypes.byref(attributes),
                                       ctypes.byref(status), None, 0x80, sharing,
                                       3 if create and not directory else 1,
                                       0x00200000 | 0x20 | 0x4000, None, 0)
            if code < 0:
                raise ctypes.WinError(native.RtlNtStatusToDosError(code))
            handle = result.value
        handles.append(handle)
        info = AttributeTag()
        if not kernel.GetFileInformationByHandleEx(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        if info.attributes & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT, including junctions
            raise OSError(errno.ELOOP, "Reparse points are not allowed")
        if bool(info.attributes & 0x10) != directory or kernel.GetFileType(handle) != 1:
            raise OSError(errno.ELOOP, "Unexpected file type")
        return handle

    try:
        parent = opened(Path(path.anchor), directory=True)
        for component in path.parts[1:-1]:
            parent = opened(component, directory=True, parent=parent)
        leaf = opened(path.name, directory=False, parent=parent)
        descriptor = msvcrt.open_osfhandle(
            leaf, (os.O_RDWR if writable else os.O_RDONLY) | os.O_BINARY | os.O_NOINHERIT
        )
        handles.pop()  # Ownership transferred to the C runtime descriptor.
        return descriptor
    finally:
        for handle in reversed(handles):
            kernel.CloseHandle(handle)


def lock_exclusive(stream) -> None:
    """Nonblocking process-owned lock, released when the stream closes."""
    if os.name == "nt":
        import msvcrt

        # Windows byte-range locks may extend past EOF; no sentinel write needed.
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise BlockingIOError(errno.EAGAIN, "File is already locked") from exc
            raise
    else:
        import fcntl

        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
