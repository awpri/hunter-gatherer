"""Where a database file lives, and which way of saving changes is safe there.

SQLite can protect a file against a crash half-way through a change in two
ways. With the rollback journal, readers and the writer take turns. With the
write-ahead log they work at the same time, but that is only safe when every
process using the file runs on the same computer and nothing else copies the
database's files one at a time. It is not safe on a network drive, or in a
folder that a cloud service synchronizes.

h/g decides between the two each time it opens a store. There is nothing to
configure: a database on a local disk gets the write-ahead log; one on a
network drive, in a synchronized folder, or on storage h/g cannot identify
keeps the rollback journal. The decision and its reason can be reported. Paths,
account names and identifiers never are.
"""
from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

LOCAL, NETWORK, SYNCED, UNKNOWN = "local", "network", "synced", "unknown"
WAL, ROLLBACK = "wal", "delete"
PREFERENCES = ("auto", WAL, ROLLBACK)


class UnsafeLocationError(RuntimeError):
    """Opening this database here could damage it; nothing was changed."""


@dataclass(frozen=True)
class Location:
    """What kind of storage a folder is on.

    ``detail`` names a filesystem type or a sync service, for messages. It
    never holds a path, an account name or an identifier.
    """

    kind: str
    detail: str = ""

    def describe(self) -> str:
        """Where the library is, as the end of a sentence: "on a local disk (apfs)"."""
        note = f" ({self.detail})" if self.detail else ""
        if self.kind == LOCAL:
            return "on a local disk" + note
        if self.kind == NETWORK:
            return "on a network drive" + note
        if self.kind == SYNCED:
            return "in a folder synchronized by " + (self.detail or "a cloud service")
        return "on storage h/g does not recognize" + note


# ── which filesystem ──────────────────────────────────────────────────────────
# Only filesystems known to live on a disk attached to this computer count as
# local. Anything else is treated as unsafe for the write-ahead log.
_DARWIN_LOCAL = frozenset({"apfs", "hfs", "exfat", "msdos", "ufs", "zfs", "tmpfs"})
_LINUX_LOCAL = frozenset({
    "ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "f2fs", "jfs", "reiserfs", "nilfs2", "bcachefs",
    "tmpfs", "ramfs", "overlay", "vfat", "exfat", "msdos", "ntfs3", "hfsplus", "apfs",
})
_LINUX_NETWORK = frozenset({
    "nfs", "nfs4", "cifs", "smb3", "smbfs", "ncpfs", "afs", "9p", "ceph", "glusterfs", "lustre", "gpfs",
    "beegfs", "ocfs2", "gfs2", "davfs", "virtiofs", "vboxsf", "vmhgfs", "prl_fs", "drvfs", "orangefs",
    "pvfs2", "coda", "fuse.sshfs", "fuse.rclone", "fuse.s3fs", "fuse.gcsfuse", "fuse.davfs2",
    "fuse.glusterfs", "fuse.smbnetfs", "fuse.vmhgfs-fuse", "fuse.cephfs",
})
_MNT_LOCAL = 0x00001000
_libc_handle = None


def _libc():
    global _libc_handle
    if _libc_handle is None:
        _libc_handle = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    return _libc_handle


class _DarwinStatFS(ctypes.Structure):
    _fields_ = [
        ("f_bsize", ctypes.c_uint32), ("f_iosize", ctypes.c_int32), ("f_blocks", ctypes.c_uint64),
        ("f_bfree", ctypes.c_uint64), ("f_bavail", ctypes.c_uint64), ("f_files", ctypes.c_uint64),
        ("f_ffree", ctypes.c_uint64), ("f_fsid", ctypes.c_int32 * 2), ("f_owner", ctypes.c_uint32),
        ("f_type", ctypes.c_uint32), ("f_flags", ctypes.c_uint32), ("f_fssubtype", ctypes.c_uint32),
        ("f_fstypename", ctypes.c_char * 16), ("f_mntonname", ctypes.c_char * 1024),
        ("f_mntfromname", ctypes.c_char * 1024), ("f_flags_ext", ctypes.c_uint32),
        ("f_reserved", ctypes.c_uint32 * 7),
    ]


def _darwin_filesystem(folder: Path) -> Location:
    libc = _libc()
    # Intel builds keep the 64-bit layout under a suffixed name; Apple silicon has only that layout.
    call = getattr(libc, "statfs$INODE64", None) or libc.statfs
    call.argtypes = [ctypes.c_char_p, ctypes.c_void_p]
    call.restype = ctypes.c_int
    buffer = ctypes.create_string_buffer(max(4096, ctypes.sizeof(_DarwinStatFS)))
    if call(os.fsencode(folder), buffer) != 0:
        return Location(UNKNOWN)
    info = _DarwinStatFS.from_buffer(buffer)
    name = info.f_fstypename.decode("ascii", "replace")
    if not info.f_flags & _MNT_LOCAL:
        return Location(NETWORK, name)
    return Location(LOCAL if name in _DARWIN_LOCAL else UNKNOWN, name)


def _mount_type(mountinfo: str, path: str) -> str:
    """The filesystem type of the mount that holds ``path``, from /proc/self/mountinfo."""
    best, found = -1, ""
    for line in mountinfo.splitlines():
        head, separator, tail = line.partition(" - ")
        fields, kinds = head.split(), tail.split()
        if not separator or len(fields) < 5 or not kinds:
            continue
        point = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), fields[4])
        if (path == point or path.startswith(point.rstrip("/") + "/")) and len(point) >= best:
            best, found = len(point), kinds[0]  # a later mount on the same point hides the earlier one
    return found


def _linux_filesystem(folder: Path, mountinfo: str | None = None) -> Location:
    if mountinfo is None:
        mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8", errors="replace")
    name = _mount_type(mountinfo, str(folder))
    if name in _LINUX_LOCAL:
        return Location(LOCAL, name)
    return Location(NETWORK if name in _LINUX_NETWORK else UNKNOWN, name)


def _windows_filesystem(folder: Path) -> Location:
    drive = os.path.splitdrive(str(folder))[0]
    if not drive:
        return Location(UNKNOWN)
    kind = ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(drive + "\\"))
    if kind in (2, 3, 6):  # removable, fixed, RAM disk
        return Location(LOCAL)
    return Location(NETWORK if kind == 4 else UNKNOWN)


def _filesystem(folder: Path) -> Location:
    if sys.platform == "darwin":
        return _darwin_filesystem(folder)
    if sys.platform.startswith("linux"):
        return _linux_filesystem(folder)
    if os.name == "nt":
        return _windows_filesystem(folder)
    return Location(UNKNOWN, sys.platform)


# ── which sync service ────────────────────────────────────────────────────────
# Recognized: iCloud Drive (including its Desktop & Documents option), Dropbox,
# OneDrive, Google Drive and Box where macOS manages them, the folders those
# and a few other clients create in the home directory, and folders marked by
# Dropbox, Google Drive or Syncthing. A sync tool that leaves none of these
# signs is not recognized; see docs/concurrency.md.
_HOME_FOLDERS = {
    "dropbox": "Dropbox", "onedrive": "OneDrive", "google drive": "Google Drive",
    "googledrive": "Google Drive", "my drive": "Google Drive", "icloud drive": "iCloud Drive",
    "iclouddrive": "iCloud Drive", "box": "Box", "box sync": "Box", "nextcloud": "Nextcloud",
    "owncloud": "ownCloud", "mega": "MEGA", "megasync": "MEGA", "pclouddrive": "pCloud",
    "synologydrive": "Synology Drive",
}
_HOME_PREFIXES = (("onedrive - ", "OneDrive"), ("dropbox (", "Dropbox"))
_MARKERS = ((".dropbox", "Dropbox"), (".dropbox.cache", "Dropbox"), (".stfolder", "Syncthing"),
            (".tmp.drivedownload", "Google Drive"), (".tmp.driveupload", "Google Drive"))
_SERVICE_HINTS = (("clouddocs", "iCloud Drive"), ("icloud", "iCloud Drive"), ("dropbox", "Dropbox"),
                  ("onedrive", "OneDrive"), ("google", "Google Drive"), ("drivefs", "Google Drive"),
                  ("box", "Box"), ("nextcloud", "Nextcloud"), ("owncloud", "ownCloud"))
_PROVIDER_ATTRIBUTE = "com.apple.file-provider-domain-id"


def _service_name(hint: str) -> str:
    lowered = hint.lower()
    return next((name for key, name in _SERVICE_HINTS if key in lowered), "a cloud service")


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _darwin_attribute(path: Path, name: str) -> bytes | None:
    """One extended attribute of ``path``, or None. Python has no call for this on macOS."""
    call = _libc().getxattr
    call.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t,
                     ctypes.c_uint32, ctypes.c_int]
    call.restype = ctypes.c_ssize_t
    target, attribute = os.fsencode(path), name.encode()
    size = call(target, attribute, None, 0, 0, 0)
    if size < 0:
        return None
    buffer = ctypes.create_string_buffer(max(size, 1))
    size = call(target, attribute, buffer, size, 0, 0)
    return buffer.raw[:size] if size >= 0 else None


def _darwin_sync_service(folder: Path, home: Path, chain: tuple[Path, ...]) -> str:
    library = home / "Library"
    if _within(folder, library / "Mobile Documents"):
        return "iCloud Drive"
    with contextlib.suppress(ValueError):
        provider = folder.relative_to(library / "CloudStorage").parts[:1]
        if provider:
            return _service_name(provider[0])
    for name in ("Desktop", "Documents"):  # iCloud's "Desktop & Documents Folders" option
        link = library / "Mobile Documents" / "com~apple~CloudDocs" / name
        if os.path.islink(link) and _within(folder, Path(os.path.realpath(link))):
            return "iCloud Drive"
    for ancestor in chain:
        domain = _darwin_attribute(ancestor, _PROVIDER_ATTRIBUTE)
        if domain is not None:
            return _service_name(domain.decode("utf-8", "replace"))
    return ""


def _sync_service(folder: Path) -> str:
    """The service that synchronizes ``folder``, or '' when none is recognized."""
    home = Path(os.path.realpath(Path.home()))
    chain = (folder, *folder.parents)[:40]
    with contextlib.suppress(ValueError):
        top = folder.relative_to(home).parts[:1]
        name = top[0].lower() if top else ""
        if name in _HOME_FOLDERS:
            return _HOME_FOLDERS[name]
        for prefix, service in _HOME_PREFIXES:
            if name.startswith(prefix):
                return service
    for ancestor in chain:
        for marker, service in _MARKERS:
            if os.path.lexists(ancestor / marker):
                return service
    if os.name == "nt":
        for variable in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
            root = os.environ.get(variable)
            if root and _within(folder, Path(os.path.realpath(root))):
                return "OneDrive"
    if sys.platform == "darwin":
        return _darwin_sync_service(folder, home, chain)
    return ""


def classify(path: str | os.PathLike) -> Location:
    """Classify the folder that holds a database file, or will hold it.

    Never raises. Whatever cannot be worked out is ``unknown``, which is
    treated like the unsafe cases.
    """
    try:
        folder = Path(os.path.realpath(os.path.expanduser(os.fspath(path))))
        while not folder.is_dir() and folder != folder.parent:
            folder = folder.parent
        filesystem = _filesystem(folder)
        if filesystem.kind != LOCAL:
            return filesystem
        service = _sync_service(folder)
        return Location(SYNCED, service) if service else filesystem
    except Exception:  # noqa: BLE001 - a classification problem must not stop a library opening
        return Location(UNKNOWN)


# ── the journal that suits a location ─────────────────────────────────────────
def preferred_journal(location: Location, preference: str = "auto") -> str:
    """``wal`` or ``delete``. ``preference`` pins a mode for tests and measurements."""
    if preference not in PREFERENCES:
        raise ValueError("sqlite_journal must be auto, wal or delete")
    return WAL if location.kind == LOCAL and preference != ROLLBACK else ROLLBACK


def journal_mode(connection: sqlite3.Connection) -> str:
    return str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()


def file_journal_mode(database: str | os.PathLike) -> str:
    """The mode recorded in a database file's header, read without opening it.

    Opening a write-ahead database, even read-only, creates its companion
    files. A report should not leave anything behind.
    """
    with open(database, "rb") as stream:
        header = stream.read(20)
    return WAL if len(header) >= 20 and header[:16] == b"SQLite format 3\x00" and header[18] == 2 else ROLLBACK


def switch_journal(connection: sqlite3.Connection, wanted: str, *, wait_ms: int = 250) -> str:
    """Try to put the database into ``wanted`` mode. Return the mode it is in.

    Changing mode needs the file to itself for a moment, and leaving the
    write-ahead log needs every other connection closed. When that is not
    possible now the database is left as it is; a later attempt can succeed.
    """
    if wanted not in (WAL, ROLLBACK):
        raise ValueError("journal mode must be wal or delete")
    current = journal_mode(connection)
    if current == wanted or connection.in_transaction:
        return current
    previous = int(connection.execute("PRAGMA busy_timeout").fetchone()[0])
    try:
        connection.execute(f"PRAGMA busy_timeout={max(0, int(wait_ms))}")
        return str(connection.execute(f"PRAGMA journal_mode={wanted}").fetchone()[0]).lower()
    except sqlite3.OperationalError:
        return journal_mode(connection)
    finally:
        connection.execute(f"PRAGMA busy_timeout={previous}")


_SHARING = {
    WAL: "readers and a writer in different processes work at the same time (write-ahead log)",
    ROLLBACK: "readers and the writer in different processes take turns (rollback journal)",
}
_WARNINGS = {
    SYNCED: "A live library in a synchronized folder can be damaged if two devices change it, or if it is "
            "uploaded while it is being written. Keep the library on a local disk and move copies with "
            "hunt backup create.",
    NETWORK: "File locking on network drives is unreliable, and two computers must never open one library "
             "at the same time. Keep the library on a local disk, or run hunt serve on one computer and "
             "connect the others to it.",
}


def report(location: Location, mode: str, preference: str = "auto") -> dict:
    """What a person needs to know about how this database is shared. No paths or IDs."""
    wanted = preferred_journal(location, preference)
    if preference == WAL and wanted == ROLLBACK:
        explanation = f"The library is {location.describe()}, so it requires the rollback journal."
    elif preference != "auto":
        explanation = f"The journal mode is pinned by configuration: {_SHARING[wanted]}."
    elif location.kind == LOCAL:
        explanation = f"The library is {location.describe()}, so {_SHARING[WAL]}."
    elif location.kind == UNKNOWN:
        explanation = (f"The library is {location.describe()}, so h/g shares it the way that is safe "
                       f"everywhere: {_SHARING[ROLLBACK]}.")
    else:
        explanation = (f"The library is {location.describe()}, where a write-ahead log is not safe, so "
                       f"{_SHARING[ROLLBACK]}.")
    pending = mode != wanted
    if pending:
        explanation += (" That has not taken effect yet: the file was in use, or has not been opened "
                        "since it was restored or moved. h/g switches it when it next has the file to "
                        "itself.")
    return {"journal_mode": mode, "location": location.kind, "location_detail": location.detail,
            "chosen": "automatically" if preference == "auto" else "by configuration",
            "explanation": explanation, "switch_pending": pending,
            "warning": _WARNINGS.get(location.kind)}


def location_warning(path: str | os.PathLike) -> str | None:
    """Why ``path`` is a risky home for a live database, or None when it is not known to be."""
    return _WARNINGS.get(classify(path).kind)


def refuse_if_unsafe(location: Location, mode: str) -> None:
    """Stop before a write-ahead database is used from somewhere other computers may reach it.

    Processes on different computers cannot share a write-ahead log. A database
    found in that mode on a network drive, and still held open by something
    else, may be in use from another computer right now.
    """
    if mode == WAL and location.kind in (NETWORK, UNKNOWN):
        raise UnsafeLocationError(
            f"This library is {location.describe()} and another program has it open in write-ahead "
            "mode, which is not safe there. Nothing was changed. Close every h/g server and assistant "
            "using this library, on this and any other computer, then try again. To use one library from "
            "two computers, run hunt serve on one and connect the other to it.")
