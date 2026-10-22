"""Native platform checks for exact-file reads and import process ownership."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hunter_gatherer.memory import secure_files
from hunter_gatherer.memory.secure_files import lock_exclusive, open_regular


def test_binary_read_and_descriptor_ownership(tmp_path):
    directory = tmp_path / "nested"
    directory.mkdir()
    path = directory / "source.txt"
    content = "Fictional café 雪\r\n".encode() + b"\x1a\x00"
    path.write_bytes(content)
    with os.fdopen(open_regular(path), "rb") as source:
        assert source.read() == content
    # No leaked ancestor/file handles prevent removing the source or directory.
    path.unlink()
    directory.rmdir()


@pytest.mark.parametrize("ancestor", [False, True], ids=["leaf", "ancestor"])
@pytest.mark.parametrize("create", [False, True], ids=["read", "create-lock"])
def test_symlink_paths_never_open_or_modify_target(tmp_path, ancestor, create):
    target = tmp_path / "outside"
    target.mkdir()
    original = target / "source.txt"
    original.write_bytes(b"outside the requested path")
    link = tmp_path / "link"
    link.symlink_to(target if ancestor else original, target_is_directory=ancestor)
    with pytest.raises(OSError):
        open_regular(link / "source.txt" if ancestor else link, writable=create, create=create)
    assert original.read_bytes() == b"outside the requested path"


def test_directories_and_relative_paths_rejected(tmp_path):
    with pytest.raises(OSError):
        open_regular(tmp_path)
    with pytest.raises(OSError):
        open_regular(Path("relative.txt"), create=True)


@pytest.mark.parametrize("create", [False, True])
def test_embedded_nul_cannot_alias_an_existing_file(tmp_path, create):
    path = tmp_path / "existing.txt"
    path.write_bytes(b"exact source")
    with pytest.raises(OSError):
        open_regular(Path(str(path) + "\x00suffix"), writable=create, create=create)
    assert path.read_bytes() == b"exact source"
    assert list(tmp_path.iterdir()) == [path]


def test_lock_excludes_another_process_and_releases_on_close(tmp_path):
    path = tmp_path / "import.lock"
    command = [sys.executable, "-I", "-c", """
import importlib.util, os, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("secure_files", sys.argv[2])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
with os.fdopen(module.open_regular(Path(sys.argv[1]), writable=True, create=True), 'a+b') as stream:
    try:
        module.lock_exclusive(stream)
    except BlockingIOError:
        sys.exit(7)
""", str(path), secure_files.__file__]
    with os.fdopen(open_regular(path, writable=True, create=True), "a+b") as stream:
        lock_exclusive(stream)
        assert subprocess.run(command, timeout=10, check=False).returncode == 7
    assert subprocess.run(command, timeout=10, check=False).returncode == 0


@pytest.mark.skipif(os.name != "nt", reason="Windows junction/reparse-point contract")
def test_windows_junction_is_rejected(tmp_path):
    target = tmp_path / "outside"
    target.mkdir()
    original = target / "source.txt"
    original.write_bytes(b"junction target")
    link = tmp_path / "junction"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                   check=True, capture_output=True, timeout=10)
    try:
        with pytest.raises(OSError):
            open_regular(link / "source.txt")
    finally:
        link.rmdir()


@pytest.mark.skipif(os.name != "nt", reason="Windows alternate data stream contract")
def test_windows_alternate_stream_and_alias_rejected(tmp_path):
    for name in ("source.txt:hidden", "source.txt.", "source.txt ", "NUL.txt", "COM1", "lpt¹.txt"):
        with pytest.raises(OSError):
            open_regular(tmp_path / name, writable=True, create=True)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows in-place reparse race contract")
def test_windows_parent_reparse_change_cannot_redirect_create(tmp_path, monkeypatch):
    """Change a checked parent without renaming it or requesting data writes."""
    import ctypes
    import struct
    from ctypes import wintypes

    parent = tmp_path / "checked-parent"
    outside = tmp_path / "outside"
    parent.mkdir()
    outside.mkdir()
    original_dll = ctypes.WinDLL
    kernel = original_dll("kernel32", use_last_error=True)
    kernel.GetFinalPathNameByHandleW.argtypes = [
        wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
    ]
    kernel.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    kernel.DeviceIoControl.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
        wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
    ]
    kernel.DeviceIoControl.restype = wintypes.BOOL
    original_info = kernel.GetFileInformationByHandleEx
    mutation_handle = None
    injected = False

    def control(code, payload):
        buffer = ctypes.create_string_buffer(payload)
        returned = wintypes.DWORD()
        if not kernel.DeviceIoControl(mutation_handle, code, buffer, len(payload),
                                      None, 0, ctypes.byref(returned), None):
            raise ctypes.WinError(ctypes.get_last_error())

    def checked_info(handle, info_class, info, size):
        nonlocal mutation_handle, injected
        original_info.argtypes = checked_info.argtypes
        original_info.restype = checked_info.restype
        result = original_info(handle, info_class, info, size)
        if not result or injected:
            return result
        buffer = ctypes.create_unicode_buffer(32768)
        length = kernel.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
        if not length or length >= len(buffer):
            raise ctypes.WinError(ctypes.get_last_error())
        opened_path = buffer.value.removeprefix("\\\\?\\")
        if os.path.normcase(opened_path) != os.path.normcase(str(parent)):
            return result
        # Attribute access bypasses share-write denial. The directory is empty,
        # so setting a junction in place needs no rename or child deletion.
        mutation_handle = kernel.CreateFileW(
            str(parent), 0x100, 0x7, None, 3, 0x02000000 | 0x00200000, None,
        )
        if mutation_handle == ctypes.c_void_p(-1).value:
            mutation_handle = None
            raise ctypes.WinError(ctypes.get_last_error())
        substitute = ("\\??\\" + str(outside)).encode("utf-16-le")
        printable = str(outside).encode("utf-16-le")
        names = substitute + b"\0\0" + printable + b"\0\0"
        data = struct.pack("<HHHH", 0, len(substitute), len(substitute) + 2,
                           len(printable)) + names
        control(0x900A4, struct.pack("<IHH", 0xA0000003, len(data), 0) + data)
        injected = True
        # Return the already-read, ordinary-directory attributes. Traversal must
        # remain safe even when this check immediately becomes stale.
        return result

    class KernelProxy:
        GetFileInformationByHandleEx = staticmethod(checked_info)

        def __getattr__(self, name):
            return getattr(kernel, name)

    def load_dll(name, *args, **kwargs):
        if str(name).casefold() == "kernel32":
            return KernelProxy()
        return original_dll(name, *args, **kwargs)

    monkeypatch.setattr(ctypes, "WinDLL", load_dll)
    descriptor = None
    try:
        try:
            descriptor = open_regular(parent / "import.lock", writable=True, create=True)
        except OSError:
            # Failing closed is also acceptable after a parent changes in place.
            pass
        assert injected, "The test must mutate the checked parent during traversal"
        assert not (outside / "import.lock").exists()
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if mutation_handle is not None:
            try:
                if injected:
                    control(0x900AC, struct.pack("<IHH", 0xA0000003, 0, 0))
            finally:
                kernel.CloseHandle(mutation_handle)
