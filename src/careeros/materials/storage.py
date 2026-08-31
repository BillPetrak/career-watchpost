"""Descriptor-relative private materials storage."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
from collections.abc import AsyncIterable, Iterable
from pathlib import Path

from careeros.materials.models import MAX_BYTES, StorageError, ValidationError

MARKER = ".careeros-store"
STAGING_RE = re.compile(r"^[0-9a-f]{64}\.part$")


def _check(st: os.stat_result, mode: int, *, regular: bool = False) -> None:
    good = stat.S_ISREG(st.st_mode) if regular else stat.S_ISDIR(st.st_mode)
    if (
        not good
        or st.st_uid != os.getuid()
        or stat.S_IMODE(st.st_mode) != mode
        or (regular and st.st_nlink != 1)
    ):
        raise StorageError("unsafe materials storage")


def _odir(parent: int, name: str, create: bool = False) -> int:
    created = False
    try:
        if create:
            try:
                os.mkdir(name, 0o700, dir_fd=parent)
                created = True
            except FileExistsError:
                pass
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        if created:
            os.fchmod(fd, 0o700)
        _check(os.fstat(fd), 0o700)
        return fd
    except OSError as exc:
        raise StorageError("materials storage unavailable") from exc


def _rootfd_if_exists(root: Path) -> int | None:
    p = Path(os.path.abspath(root))
    if _inside_git_checkout(p):
        raise StorageError("unsafe materials storage")
    try:
        pfd = _open_chain_if_exists(p.parent)
        if pfd is None:
            return None
        try:
            try:
                fd = os.open(
                    p.name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=pfd,
                )
            except FileNotFoundError:
                return None
        finally:
            os.close(pfd)
    except OSError as exc:
        raise StorageError("materials storage unavailable") from exc
    try:
        _check(os.fstat(fd), 0o700)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _rootfd(root: Path) -> int:
    fd = _rootfd_if_exists(root)
    if fd is None:
        raise StorageError("materials storage unavailable")
    return fd


def _open_chain(path: Path) -> int:
    """Open every existing component without following a symlink."""
    fd = _open_chain_if_exists(path)
    if fd is None:
        raise FileNotFoundError(os.fspath(path))
    return fd


def _open_chain_if_exists(path: Path) -> int | None:
    """Open a component chain safely, or return absent at its first missing name."""
    fd = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in path.parts[1:]:
            if component == ".":
                continue
            try:
                nxt = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=fd,
                )
            except FileNotFoundError:
                os.close(fd)
                return None
            os.close(fd)
            fd = nxt
        return fd
    except OSError:
        os.close(fd)
        raise


def _open_root_parent(path: Path, *, create: bool) -> tuple[int, bool]:
    """Open the root parent, creating only the approved ``careeros`` level."""
    try:
        return _open_chain(path.parent), False
    except OSError:
        if not create or path.parent.name != "careeros":
            raise
        pfd = _open_chain(path.parent.parent)
        try:
            os.mkdir(path.parent.name, 0o700, dir_fd=pfd)
            cfd = os.open(
                path.parent.name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=pfd,
            )
            try:
                os.fchmod(cfd, 0o700)
                os.fsync(cfd)
            finally:
                os.close(cfd)
            os.fsync(pfd)
            return _open_chain(path.parent), True
        except FileExistsError:
            raise
        finally:
            os.close(pfd)


def _inside_git_checkout(path: Path) -> bool:
    current = path if path.is_absolute() else Path(os.path.abspath(path))
    for candidate in (current, *current.parents):
        try:
            marker = candidate / ".git"
            if marker.is_symlink():
                return True
            if marker.is_dir() and (marker / "HEAD").is_file():
                return True
            if marker.is_file():
                return marker.read_text(errors="ignore").startswith("gitdir:")
        except OSError:
            return True
    return False


def prepare_root(root: Path, *, create: bool = True) -> None:
    p = Path(os.path.abspath(root))
    if _inside_git_checkout(p):
        raise StorageError("unsafe materials storage")
    try:
        pfd, _ = _open_root_parent(p, create=create)
    except OSError as exc:
        raise StorageError("materials storage unavailable") from exc
    try:
        root_created = False
        if create:
            try:
                os.mkdir(p.name, 0o700, dir_fd=pfd)
                root_created = True
            except FileExistsError:
                pass
        rfd = os.open(p.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=pfd)
        if root_created:
            os.fchmod(rfd, 0o700)
        _check(os.fstat(rfd), 0o700)
        try:
            for name in (".staging", "objects"):
                d = _odir(rfd, name, create)
                os.close(d)
            if create or root_created:
                os.fsync(rfd)
                os.fsync(pfd)
        finally:
            os.close(rfd)
    except OSError as exc:
        raise StorageError("materials storage unavailable") from exc
    finally:
        os.close(pfd)


def marker_id(root: Path) -> str | None:
    rfd = _rootfd(root)
    try:
        return marker_id_fd(rfd)
    finally:
        os.close(rfd)


def marker_id_fd(rfd: int) -> str | None:
    try:
        fd = os.open(
            MARKER,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=rfd,
        )
    except FileNotFoundError:
        return None
    try:
        _check(os.fstat(fd), 0o600, regular=True)
        if os.fstat(fd).st_size > 128:
            raise StorageError("unsafe materials storage")
        raw = b""
        while chunk := os.read(fd, 4096):
            raw += chunk
            if len(raw) > 128:
                raise StorageError("unsafe materials storage")
    finally:
        os.close(fd)
    match = re.fullmatch(rb"careeros-material-store-v1:([0-9a-f]{32})\n", raw)
    if not match:
        raise StorageError("unsafe materials storage")
    return match.group(1).decode()


def _populated(rfd: int) -> bool:
    ofd = _odir(rfd, "objects")
    try:
        for a in os.listdir(ofd):
            if not re.fullmatch(r"[0-9a-f]{2}", a):
                raise StorageError("unsafe materials storage")
            afd = _odir(ofd, a)
            try:
                for b in os.listdir(afd):
                    if not re.fullmatch(r"[0-9a-f]{2}", b):
                        raise StorageError("unsafe materials storage")
                    bfd = _odir(afd, b)
                    try:
                        if os.listdir(bfd):
                            return True
                    finally:
                        os.close(bfd)
            finally:
                os.close(afd)
        return False
    finally:
        os.close(ofd)


def _validate_optional_directory(parent: int, name: str) -> None:
    """Validate an optional managed directory without following its entry."""
    try:
        fd = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent,
        )
    except FileNotFoundError:
        return
    except OSError as exc:
        raise StorageError("materials storage unavailable") from exc
    try:
        _check(os.fstat(fd), 0o700)
    finally:
        os.close(fd)


def ensure_binding(
    root: Path, db_store_id: str | None, *, allow_create: bool = True
) -> str | None:
    if not allow_create:
        rfd = _rootfd_if_exists(root)
        if rfd is None:
            if db_store_id:
                raise StorageError("materials storage binding unavailable") from None
            return None
        try:
            entries = os.listdir(rfd)
            if not entries:
                if db_store_id:
                    raise StorageError("materials storage binding unavailable")
                return None
            return ensure_binding_fd(rfd, root, db_store_id)
        finally:
            os.close(rfd)
    try:
        prepare_root(root, create=True)
    except StorageError:
        raise
    rfd = _rootfd(root)
    try:
        return ensure_binding_fd(rfd, root, db_store_id)
    finally:
        os.close(rfd)


def ensure_binding_fd(rfd: int, root: Path, db_store_id: str | None) -> str | None:
    """Validate binding against an already-held root descriptor."""
    st = os.fstat(rfd)
    _check(st, 0o700)
    try:
        configured = _rootfd(root)
    except OSError as exc:
        raise StorageError("materials storage binding unavailable") from exc
    try:
        cst = os.fstat(configured)
    finally:
        os.close(configured)
    if (st.st_dev, st.st_ino) != (cst.st_dev, cst.st_ino):
        raise StorageError("materials storage binding unavailable")
    extras = set(os.listdir(rfd)) - {MARKER, "objects", ".staging"}
    if extras:
        raise StorageError("unsafe materials storage")
    _validate_optional_directory(rfd, ".staging")
    populated = _populated(rfd)
    found = marker_id_fd(rfd)
    if db_store_id and found != db_store_id:
        raise StorageError("materials storage binding unavailable")
    if not db_store_id and populated:
        raise StorageError("materials storage binding unavailable")
    return found


def install_marker(root: Path, store_id: str) -> None:
    prepare_root(root)
    rfd = _rootfd(root)
    try:
        install_marker_fd(rfd, store_id)
    finally:
        os.close(rfd)


def install_marker_fd(rfd: int, store_id: str) -> None:
    tmp = f".{secrets.token_hex(16)}.marker"
    try:
        fd = os.open(
            tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=rfd
        )
        try:
            payload = f"careeros-material-store-v1:{store_id}\n".encode()
            _write(fd, payload)
            os.fchmod(fd, 0o600)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.link(tmp, MARKER, src_dir_fd=rfd, dst_dir_fd=rfd, follow_symlinks=False)
        os.unlink(tmp, dir_fd=rfd)
        os.fsync(rfd)
    except FileExistsError:
        try:
            os.unlink(tmp, dir_fd=rfd)
        except FileNotFoundError:
            pass
        if marker_id_fd(rfd) != store_id:
            raise StorageError("materials storage binding unavailable") from None
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=rfd)
            os.fsync(rfd)
        except OSError:
            pass
        raise


def remove_marker(root: Path) -> None:
    rfd = _rootfd(root)
    try:
        try:
            os.unlink(MARKER, dir_fd=rfd)
            os.fsync(rfd)
        except FileNotFoundError:
            pass
    finally:
        os.close(rfd)


def open_root_handle(root: Path) -> int:
    """Hold a validated root descriptor for operation-scoped cleanup."""
    return _rootfd(root)


def remove_marker_fd(rfd: int) -> None:
    """Remove a marker from an already-held root descriptor."""
    try:
        try:
            os.unlink(MARKER, dir_fd=rfd)
            os.fsync(rfd)
        except FileNotFoundError:
            pass
    except OSError as exc:
        raise StorageError("materials storage unavailable") from exc


def _write(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        view = view[n:]


def stage_bytes(root: Path, chunks: Iterable[bytes]) -> tuple[Path, int, str]:
    prepare_root(root)
    rfd = _rootfd(root)
    sfd = _odir(rfd, ".staging")
    name = f"{secrets.token_hex(32)}.part"
    fd = os.open(
        name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=sfd
    )
    h = hashlib.sha256()
    size = 0
    try:
        for chunk in chunks:
            if size + len(chunk) > MAX_BYTES:
                raise ValidationError("file too large")
            _write(fd, chunk)
            h.update(chunk)
            size += len(chunk)
        if not size:
            raise ValidationError("empty file")
        os.fchmod(fd, 0o600)
        os.fsync(fd)
        os.fsync(sfd)
    except Exception:
        os.close(fd)
        os.unlink(name, dir_fd=sfd)
        os.close(sfd)
        os.close(rfd)
        raise
    os.close(fd)
    os.close(sfd)
    os.close(rfd)
    return Path(root) / ".staging" / name, size, h.hexdigest()


def stage_source(root: Path, source: Path) -> tuple[Path, int, str]:
    """Stage a regular source file while detecting mutation during the copy."""
    try:
        src = os.open(
            source,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
        )
    except OSError as exc:
        raise StorageError("source unavailable") from exc
    try:
        before = os.fstat(src)
        _check_source(before)
        result = _stage_fd(root, src)
        after = os.fstat(src)
        if _source_tuple(before) != _source_tuple(after):
            remove_stage(result[0])
            raise StorageError("source changed during copy")
        return result
    finally:
        os.close(src)


def _check_source(st: os.stat_result) -> None:
    if not stat.S_ISREG(st.st_mode):
        raise StorageError("source unavailable")


def _source_tuple(st: os.stat_result) -> tuple[int, ...]:
    return (
        st.st_dev,
        st.st_ino,
        st.st_size,
        st.st_mode,
        st.st_uid,
        st.st_nlink,
        st.st_mtime_ns,
        st.st_ctime_ns,
    )


def _stage_fd(root: Path, src: int) -> tuple[Path, int, str]:
    prepare_root(root)
    rfd = _rootfd(root)
    sfd = _odir(rfd, ".staging")
    name = f"{secrets.token_hex(32)}.part"
    dst = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=sfd,
    )
    digest = hashlib.sha256()
    size = 0
    try:
        while True:
            chunk = os.read(src, 1024 * 1024)
            if not chunk:
                break
            if size + len(chunk) > MAX_BYTES:
                raise ValidationError("file too large")
            _write(dst, chunk)
            digest.update(chunk)
            size += len(chunk)
        if not size:
            raise ValidationError("empty file")
        os.fchmod(dst, 0o600)
        os.fsync(dst)
        os.fsync(sfd)
        return Path(root) / ".staging" / name, size, digest.hexdigest()
    except BaseException:
        try:
            os.close(dst)
        except OSError:
            pass
        try:
            os.unlink(name, dir_fd=sfd)
            os.fsync(sfd)
        except FileNotFoundError:
            pass
        raise
    finally:
        try:
            os.close(dst)
        except OSError:
            pass
        try:
            os.close(sfd)
            os.close(rfd)
        except OSError:
            pass


async def stage_stream(
    root: Path, chunks: AsyncIterable[bytes]
) -> tuple[Path, int, str]:
    prepare_root(root)
    rfd = _rootfd(root)
    sfd = _odir(rfd, ".staging")
    name = f"{secrets.token_hex(32)}.part"
    fd = os.open(
        name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=sfd
    )
    digest = hashlib.sha256()
    size = 0
    try:
        async for chunk in chunks:
            if size + len(chunk) > MAX_BYTES:
                raise ValidationError("file too large")
            _write(fd, chunk)
            digest.update(chunk)
            size += len(chunk)
        if not size:
            raise ValidationError("empty file")
        os.fchmod(fd, 0o600)
        os.fsync(fd)
        os.fsync(sfd)
    except BaseException:
        try:
            os.close(fd)
        finally:
            try:
                os.unlink(name, dir_fd=sfd)
                os.fsync(sfd)
            finally:
                os.close(sfd)
                os.close(rfd)
        raise
    os.close(fd)
    os.close(sfd)
    os.close(rfd)
    return Path(root) / ".staging" / name, size, digest.hexdigest()


def remove_stage(stage: Path) -> None:
    rfd = _rootfd(stage.parents[1])
    sfd = _odir(rfd, ".staging")
    try:
        try:
            os.unlink(stage.name, dir_fd=sfd)
            os.fsync(sfd)
        except FileNotFoundError:
            pass
    finally:
        os.close(sfd)
        os.close(rfd)


def stage_identity(stage: Path) -> tuple[int, ...]:
    rfd = _rootfd(stage.parents[1])
    sfd = _odir(rfd, ".staging")
    fd = -1
    try:
        fd = os.open(
            stage.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=sfd,
        )
        st = os.fstat(fd)
        _check(st, 0o600, regular=True)
        return _source_tuple(st)
    except OSError as exc:
        raise StorageError("invalid staged material") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(sfd)
        os.close(rfd)


def verify_stage(
    stage: Path, expected_size: int, expected_identity: tuple[int, ...]
) -> None:
    """Validate a staged inode under lock without reading its bytes."""
    rfd = _rootfd(stage.parents[1])
    sfd = _odir(rfd, ".staging")
    fd = -1
    try:
        fd = os.open(
            stage.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=sfd,
        )
        st = os.fstat(fd)
        _check(st, 0o600, regular=True)
        if st.st_size != expected_size or _source_tuple(st) != expected_identity:
            raise ValidationError("invalid staged material")
    except OSError as exc:
        raise StorageError("invalid staged material") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(sfd)
        os.close(rfd)


def remove_object(path: Path) -> None:
    rfd = _rootfd(path.parents[3])
    ofd = _odir(rfd, "objects")
    afd = _odir(ofd, path.parts[-3])
    bfd = _odir(afd, path.parts[-2])
    try:
        os.unlink(path.name, dir_fd=bfd)
        os.fsync(bfd)
    except FileNotFoundError:
        pass
    finally:
        for fd in (bfd, afd, ofd, rfd):
            os.close(fd)


def remove_object_fd(rfd: int, key: str) -> None:
    """Remove an object through an operation-held root descriptor."""
    if not re.fullmatch(r"[0-9a-f]{64}", key):
        raise StorageError("invalid storage object")
    ofd = _odir(rfd, "objects")
    afd = _odir(ofd, key[:2])
    bfd = _odir(afd, key[2:4])
    try:
        try:
            os.unlink(f"{key}.blob", dir_fd=bfd)
            os.fsync(bfd)
        except FileNotFoundError:
            pass
    finally:
        os.close(bfd)
        os.close(afd)
        os.close(ofd)


def finalize_stage(
    root: Path,
    stage: Path,
    storage_key: str,
    *,
    expected_size: int | None = None,
    expected_identity: tuple[int, ...] | None = None,
    root_fd: int | None = None,
    expected_store_id: str | None = None,
) -> Path:
    if not STAGING_RE.fullmatch(stage.name) or not re.fullmatch(
        r"[0-9a-f]{64}", storage_key
    ):
        raise StorageError("invalid staging file")
    owned_root = root_fd is None
    rfd = _rootfd(root) if owned_root else root_fd
    assert rfd is not None
    root_st = os.fstat(rfd)
    sfd = _odir(rfd, ".staging")
    ofd = _odir(rfd, "objects")
    a = b = None
    installed = False
    try:
        if expected_store_id is not None and marker_id_fd(rfd) != expected_store_id:
            raise StorageError("materials storage binding unavailable")
        stage_fd = os.open(
            stage.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=sfd,
        )
        try:
            st = os.fstat(stage_fd)
            _check(st, 0o600, regular=True)
            if expected_size is not None and st.st_size != expected_size:
                raise StorageError("invalid staged material")
            if expected_identity is not None and _source_tuple(st) != expected_identity:
                raise StorageError("invalid staged material")
        finally:
            os.close(stage_fd)
        shard_a = storage_key[:2]
        shard_b = storage_key[2:4]
        try:
            os.stat(shard_a, dir_fd=ofd, follow_symlinks=False)
            a_new = False
        except FileNotFoundError:
            a_new = True
        a = _odir(ofd, shard_a, True)
        if a_new:
            os.fsync(ofd)
        try:
            os.stat(shard_b, dir_fd=a, follow_symlinks=False)
            b_new = False
        except FileNotFoundError:
            b_new = True
        b = _odir(a, shard_b, True)
        if b_new:
            os.fsync(a)
        name = f"{storage_key}.blob"
        os.link(stage.name, name, src_dir_fd=sfd, dst_dir_fd=b, follow_symlinks=False)
        installed = True
        os.unlink(stage.name, dir_fd=sfd)
        fd = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=b,
        )
        try:
            os.fchmod(fd, 0o600)
            final_stat = os.fstat(fd)
            # The staging name must be gone before the database can refer to
            # this object.  Explicitly enforce the one-link invariant rather
            # than relying on the preceding unlink succeeding silently.
            _check(final_stat, 0o600, regular=True)
            if expected_size is not None and final_stat.st_size != expected_size:
                raise StorageError("invalid staged material")
            # Linking/unlinking necessarily updates ctime and nlink during
            # installation; compare all pre-staging identity fields that must
            # remain stable while allowing those link-induced transitions.
            if expected_identity is not None and (
                final_stat.st_dev,
                final_stat.st_ino,
                final_stat.st_size,
                final_stat.st_mode,
                final_stat.st_uid,
                final_stat.st_mtime_ns,
            ) != (
                expected_identity[0],
                expected_identity[1],
                expected_identity[2],
                expected_identity[3],
                expected_identity[4],
                expected_identity[6],
            ):
                raise StorageError("invalid staged material")
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(b)
        os.fsync(a)
        os.fsync(ofd)
        os.fsync(sfd)
        current_root = os.fstat(rfd)
        configured_root = _rootfd(root)
        try:
            configured_st = os.fstat(configured_root)
        finally:
            os.close(configured_root)
        if (
            current_root.st_dev != root_st.st_dev
            or current_root.st_ino != root_st.st_ino
            or configured_st.st_dev != root_st.st_dev
            or configured_st.st_ino != root_st.st_ino
        ):
            raise StorageError("materials storage binding unavailable")
        return Path(root) / "objects" / storage_key[:2] / storage_key[2:4] / name
    except Exception:
        if installed and b is not None:
            try:
                os.unlink(f"{storage_key}.blob", dir_fd=b)
                os.fsync(b)
            except OSError:
                pass
        raise
    finally:
        for maybe_fd in (b, a, ofd, sfd):
            if maybe_fd is not None:
                try:
                    os.close(maybe_fd)
                except OSError:
                    pass
        if owned_root:
            os.close(rfd)


def content_path(root: Path, key: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{64}", key):
        raise StorageError("invalid storage object")
    return Path(root) / "objects" / key[:2] / key[2:4] / f"{key}.blob"


def open_object(root: Path, key: str) -> int:
    """Open a managed object through held no-follow directory descriptors."""
    if not re.fullmatch(r"[0-9a-f]{64}", key):
        raise StorageError("invalid storage object")
    rfd = _rootfd(root)
    ofd = afd = bfd = -1
    try:
        ofd = _odir(rfd, "objects")
        afd = _odir(ofd, key[:2])
        bfd = _odir(afd, key[2:4])
        fd = os.open(
            f"{key}.blob",
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=bfd,
        )
        return fd
    except OSError as exc:
        raise StorageError("material content unavailable") from exc
    finally:
        for descriptor in (bfd, afd, ofd, rfd):
            if descriptor >= 0:
                os.close(descriptor)


def open_bound_object(root: Path, store_id: str, key: str) -> int:
    """Validate a store and open an object from that same held root.

    Binding validation and object traversal deliberately share ``rfd``.  The
    configured path is checked once more after opening the object, but a
    replacement after that check cannot redirect the already-open object FD.
    """
    if not re.fullmatch(r"[0-9a-f]{64}", key):
        raise StorageError("invalid storage object")
    rfd = _rootfd(root)
    ofd = afd = bfd = -1
    fd = -1
    try:
        if ensure_binding_fd(rfd, root, store_id) != store_id:
            raise StorageError("materials storage binding unavailable")
        ofd = _odir(rfd, "objects")
        afd = _odir(ofd, key[:2])
        bfd = _odir(afd, key[2:4])
        fd = os.open(
            f"{key}.blob",
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=bfd,
        )
        held = os.fstat(rfd)
        configured = _rootfd(root)
        try:
            current = os.fstat(configured)
        finally:
            os.close(configured)
        if (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino):
            raise StorageError("materials storage binding unavailable")
        return fd
    except OSError as exc:
        if fd >= 0:
            os.close(fd)
        raise StorageError("material content unavailable") from exc
    except BaseException:
        if fd >= 0:
            os.close(fd)
        raise
    finally:
        for descriptor in (bfd, afd, ofd, rfd):
            if descriptor >= 0:
                os.close(descriptor)


def root_identity(root: Path) -> tuple[int, int]:
    fd = _rootfd(root)
    try:
        st = os.fstat(fd)
        return st.st_dev, st.st_ino
    finally:
        os.close(fd)
