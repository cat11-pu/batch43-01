"""Root-confined path resolution and atomic writes.

The warden deliberately does not trust a path after it has been checked.
Writes are performed relative to directory file descriptors with
``O_NOFOLLOW`` so a rename or symlink swap cannot redirect the operation to a
different directory between validation and use.
"""

from __future__ import annotations

import errno as _errno
import itertools
import ntpath
import os
import posixpath
import secrets
import stat
import unicodedata


class PathConfinementError(Exception):
    """Base failure raised by the warden."""

    def __init__(self, code, message="", *, path=None):
        self.code = code
        self.path = path
        super().__init__(message or code)

    @property
    def relative(self):
        return self.path


class PathEscapeError(PathConfinementError):
    """Raised when a path escapes the confined root."""


class PathNotBeneath(PathEscapeError):
    """Raised for absolute paths or parent-directory components."""


class PathStorageError(PathConfinementError):
    """Raised for failures of the underlying filesystem.

    Keeping this distinct from :class:`PathEscapeError` is important: a full
    disk and an attempted path escape must not be reported as the same kind of
    event.
    """

    def __init__(self, message="", *, errno=None, path=None):
        self.errno = errno
        code = "storage_no_space" if errno == _errno.ENOSPC else "storage_error"
        super().__init__(code, message, path=path)


class PathConflictError(PathEscapeError):
    """Raised when archive entries would alias the same destination."""


_DIR_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_FILE_OPEN_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_UNIQUE_COUNTER = itertools.count()


class _FailAfterTemp(Exception):
    """Internal sentinel used by the deterministic failure test hook."""


def _as_text(value):
    value = os.fspath(value)
    if isinstance(value, bytes):
        value = os.fsdecode(value)
    return unicodedata.normalize("NFC", value)


def _is_absolute(path):
    """Return True for POSIX and Windows absolute/drive-relative paths."""

    drive, _ = ntpath.splitdrive(path)
    return bool(drive) or ntpath.isabs(path) or posixpath.isabs(path)


def _raw_components(path):
    """Split an absolute or relative path without resolving ``..``."""

    path = path.replace("\\", "/")
    return [part for part in path.split("/") if part not in ("", ".")]


def _is_magic_link_path(components):
    lowered = [part.casefold() for part in components]
    for index, part in enumerate(lowered):
        if part == "proc" and index + 2 < len(lowered):
            middle = lowered[index + 1]
            if (middle in ("self", "thread-self") or middle.isdigit()) and (
                lowered[index + 2] in ("fd", "fdinfo")
            ):
                return True
        if part == "dev" and index + 1 < len(lowered):
            if lowered[index + 1] == "fd":
                return True
    return False


def _absolute_target_components(root, target):
    """Return components of an absolute target relative to ``root``."""

    windows_style = bool(ntpath.splitdrive(target)[0]) or "\\" in target
    if windows_style:
        root_norm = ntpath.normcase(ntpath.normpath(root))
        target_norm = ntpath.normcase(ntpath.normpath(target))
        try:
            common = ntpath.commonpath([root_norm, target_norm])
        except ValueError:
            common = None
        if common != root_norm:
            raise PathEscapeError(
                "path_escape",
                "symbolic link target is outside the confined root: %r" % target,
                path=target,
            )
        suffix = target_norm[len(root_norm) :]
    else:
        root_norm = posixpath.normpath(root)
        target_norm = posixpath.normpath(target)
        try:
            common = posixpath.commonpath([root_norm, target_norm])
        except ValueError:
            common = None
        if common != root_norm:
            raise PathEscapeError(
                "path_escape",
                "symbolic link target is outside the confined root: %r" % target,
                path=target,
            )
        suffix = target_norm[len(root_norm) :]

    return tuple(part for part in suffix.replace("\\", "/").split("/") if part)


def _resolve_static_symlinks(root, components, symlinks, original_path):
    """Expand a caller-supplied symlink model without touching the filesystem."""

    if not symlinks:
        return tuple(components)

    normalized = {}
    for name, target in symlinks.items():
        name = _as_text(name).replace("\\", "/").strip("/")
        target = _as_text(target)
        normalized[name] = target

    def walk(items, stack, through_symlink, depth):
        if depth > 64:
            raise PathEscapeError(
                "path_escape",
                "too many symbolic links while resolving %r" % original_path,
                path=original_path,
            )

        index = 0
        while index < len(items):
            part = items[index]
            if part in ("", "."):
                index += 1
                continue
            if part == "..":
                if not stack:
                    error = PathEscapeError if through_symlink else PathNotBeneath
                    raise error(
                        "path_escape" if through_symlink else "path_not_beneath",
                        "path is not beneath root: %r" % original_path,
                        path=original_path,
                    )
                stack = stack[:-1]
                index += 1
                continue

            logical = "/".join(stack + [part])
            target = normalized.get(logical)
            if target is None:
                target = normalized.get(part)

            if target is not None:
                if _is_magic_link_path(_raw_components(target)):
                    raise PathEscapeError(
                        "path_escape",
                        "magic process file-descriptor links are not permitted: %r"
                        % original_path,
                        path=original_path,
                    )
                if _is_absolute(target):
                    try:
                        target_parts = _absolute_target_components(root, target)
                    except PathEscapeError as exc:
                        raise PathEscapeError(
                            "path_escape",
                            "symbolic link target %r is outside the confined "
                            "root while resolving %r" % (target, original_path),
                            path=original_path,
                        ) from exc
                    return walk(
                        list(target_parts) + items[index + 1 :],
                        stack,
                        True,
                        depth + 1,
                    )
                target_parts = _raw_components(target)
                return walk(
                    list(target_parts) + items[index + 1 :],
                    stack,
                    True,
                    depth + 1,
                )

            stack = stack + [part]
            index += 1
        return tuple(stack)

    return walk(list(components), [], False, 0)


class PathConfinementWarden:
    """Resolves and writes paths without ever leaving the provided root."""

    def __init__(self, max_path_length=255):
        self.max_path_length = max_path_length

    def _components(self, relative, *, allow_root=False):
        text = _as_text(relative)
        if "\x00" in text:
            raise PathEscapeError(
                "path_escape", "path contains a NUL byte: %r" % text, path=text
            )
        if _is_absolute(text):
            raise PathNotBeneath(
                "path_not_beneath",
                "absolute paths are not beneath the confined root: %r" % text,
                path=text,
            )

        raw_parts = text.replace("\\", "/").split("/")
        if _is_magic_link_path(raw_parts):
            raise PathEscapeError(
                "path_escape",
                "magic process file-descriptor links are not permitted: %r" % text,
                path=text,
            )

        components = []
        for part in raw_parts:
            if part in ("", "."):
                continue
            if part == "..":
                if not components:
                    raise PathNotBeneath(
                        "path_not_beneath",
                        "path is not beneath the confined root: %r" % text,
                        path=text,
                    )
                components.pop()
                continue
            if len(part) > self.max_path_length:
                raise PathEscapeError(
                    "path_escape",
                    "path component is too long: %r" % text,
                    path=text,
                )
            components.append(part)

        if not components and not allow_root:
            raise PathEscapeError(
                "path_escape", "empty path does not name a file: %r" % text, path=text
            )

        joined = "/".join(components)
        if len(joined) > self.max_path_length:
            raise PathEscapeError(
                "path_escape", "path is too long: %r" % text, path=text
            )
        if _is_magic_link_path(components):
            raise PathEscapeError(
                "path_escape",
                "magic process file-descriptor links are not permitted: %r" % text,
                path=text,
            )
        return tuple(components)

    @staticmethod
    def _open_root(root):
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            return os.open(root, flags)
        except OSError as exc:
            raise PathStorageError(
                "cannot open extraction root %r: %s" % (root, exc.strerror),
                errno=exc.errno,
                path=root,
            ) from exc

    @staticmethod
    def _walk_directories(
        root_fd, root_dev, components, *, create, path, created_paths=None
    ):
        """Open a directory chain, one component at a time, without following."""

        current = os.dup(root_fd)
        created = []
        try:
            for index, part in enumerate(components):
                try:
                    next_fd = os.open(part, _DIR_OPEN_FLAGS, dir_fd=current)
                except FileNotFoundError:
                    if not create:
                        raise
                    try:
                        os.mkdir(part, 0o777, dir_fd=current)
                    except FileExistsError:
                        pass
                    else:
                        created.append((os.dup(current), part))
                        if created_paths is not None:
                            created_paths.append(tuple(components[: index + 1]))
                    try:
                        next_fd = os.open(part, _DIR_OPEN_FLAGS, dir_fd=current)
                    except OSError as exc:
                        if exc.errno in (_errno.ELOOP, _errno.ENOTDIR):
                            raise PathEscapeError(
                                "path_escape",
                                "directory component was replaced by a symlink: %r"
                                % path,
                                path=path,
                            ) from exc
                        raise
                except OSError as exc:
                    if exc.errno in (_errno.ELOOP, _errno.ENOTDIR):
                        raise PathEscapeError(
                            "path_escape",
                            "directory component is a symlink or not a directory: %r"
                            % path,
                            path=path,
                        ) from exc
                    raise

                try:
                    st = os.fstat(next_fd)
                    if not stat.S_ISDIR(st.st_mode):
                        raise PathEscapeError(
                            "path_escape",
                            "directory component is not a directory: %r" % path,
                            path=path,
                        )
                    if st.st_dev != root_dev:
                        raise PathEscapeError(
                            "path_escape",
                            "path crosses a mount point: %r" % path,
                            path=path,
                        )
                except Exception:
                    os.close(next_fd)
                    raise

                os.close(current)
                current = next_fd
            return current, created
        except Exception:
            os.close(current)
            for parent_fd, name in reversed(created):
                try:
                    os.rmdir(name, dir_fd=parent_fd)
                except OSError:
                    pass
                finally:
                    os.close(parent_fd)
            raise

    @staticmethod
    def _discard_created_directories(created):
        for parent_fd, name in reversed(created):
            try:
                os.rmdir(name, dir_fd=parent_fd)
            except (OSError, PathConfinementError):
                pass
            finally:
                os.close(parent_fd)

    @staticmethod
    def _close_created_trackers(created):
        for parent_fd, _ in created:
            os.close(parent_fd)

    @staticmethod
    def _remove_empty_created_directories(
        root_fd, root_dev, created_paths, path
    ):
        seen = set()
        ordered = sorted(created_paths, key=len, reverse=True)
        for components in ordered:
            if components in seen:
                continue
            seen.add(components)
            parent_fd = None
            try:
                parent_fd, _ = PathConfinementWarden._walk_directories(
                    root_fd,
                    root_dev,
                    components[:-1],
                    create=False,
                    path=path,
                )
                os.rmdir(components[-1], dir_fd=parent_fd)
            except (OSError, PathConfinementError):
                pass
            finally:
                if parent_fd is not None:
                    os.close(parent_fd)

    @staticmethod
    def _create_stage(root_fd, root_dev, path):
        while True:
            stage_name = ".warden-stage-%s-%d" % (
                secrets.token_hex(16),
                next(_UNIQUE_COUNTER),
            )
            try:
                os.mkdir(stage_name, 0o700, dir_fd=root_fd)
            except FileExistsError:
                continue
            try:
                stage_fd = os.open(stage_name, _DIR_OPEN_FLAGS, dir_fd=root_fd)
                st = os.fstat(stage_fd)
                if st.st_dev != root_dev:
                    raise PathEscapeError(
                        "path_escape",
                        "staging directory crosses a mount point: %r" % path,
                        path=path,
                    )
                return stage_name, stage_fd
            except Exception:
                try:
                    os.rmdir(stage_name, dir_fd=root_fd)
                except OSError:
                    pass
                raise

    @staticmethod
    def _remove_stage(root_fd, stage_name, stage_fd):
        if stage_fd is None:
            return
        try:
            for name in os.listdir(stage_fd):
                try:
                    os.unlink(name, dir_fd=stage_fd)
                except OSError:
                    pass
        finally:
            os.close(stage_fd)
        try:
            os.rmdir(stage_name, dir_fd=root_fd)
        except OSError:
            pass

    @staticmethod
    def _write_stage_file(stage_fd, name, data):
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        file_fd = os.open(name, flags, 0o600, dir_fd=stage_fd)
        try:
            view = memoryview(data)
            while view:
                written = os.write(file_fd, view)
                if written <= 0:
                    raise OSError(_errno.EIO, "short write")
                view = view[written:]
            os.fsync(file_fd)
        finally:
            os.close(file_fd)

    @staticmethod
    def _verify_directory_identity(
        root, root_fd, parent_fd, components, path, root_is_symlink
    ):
        """Ensure the opened parent is still reachable at its in-root name."""

        try:
            root_now = os.stat(root, follow_symlinks=root_is_symlink)
        except OSError as exc:
            raise PathEscapeError(
                "path_escape",
                "extraction root changed during extraction: %r" % path,
                path=path,
            ) from exc
        root_opened = os.fstat(root_fd)
        if (not root_is_symlink and stat.S_ISLNK(root_now.st_mode)) or (
            root_now.st_dev,
            root_now.st_ino,
        ) != (root_opened.st_dev, root_opened.st_ino):
            raise PathEscapeError(
                "path_escape",
                "extraction root was replaced during extraction: %r" % path,
                path=path,
            )

        current = os.dup(root_fd)
        try:
            for part in components:
                try:
                    next_fd = os.open(part, _DIR_OPEN_FLAGS, dir_fd=current)
                except OSError as exc:
                    raise PathEscapeError(
                        "path_escape",
                        "target directory changed during extraction: %r" % path,
                        path=path,
                    ) from exc
                os.close(current)
                current = next_fd

            expected = os.fstat(current)
            actual = os.fstat(parent_fd)
            if (expected.st_dev, expected.st_ino) != (
                actual.st_dev,
                actual.st_ino,
            ):
                raise PathEscapeError(
                    "path_escape",
                    "target directory was replaced during extraction: %r" % path,
                    path=path,
                )
        finally:
            os.close(current)

    def _verify_existing_path(self, root, components, original_path):
        """Best-effort side-effect-free check used by ``resolve``."""

        try:
            root_fd = os.open(
                root,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
        except FileNotFoundError:
            return
        except OSError as exc:
            raise PathStorageError(
                "cannot inspect extraction root %r: %s" % (root, exc.strerror),
                errno=exc.errno,
                path=root,
            ) from exc

        current = root_fd
        try:
            root_dev = os.fstat(root_fd).st_dev
            for index, part in enumerate(components):
                try:
                    st = os.stat(part, dir_fd=current, follow_symlinks=False)
                except FileNotFoundError:
                    return
                except OSError as exc:
                    if exc.errno in (_errno.ELOOP, _errno.ENOTDIR):
                        raise PathEscapeError(
                            "path_escape",
                            "path contains a symlink or invalid component: %r"
                            % original_path,
                            path=original_path,
                        ) from exc
                    raise PathStorageError(
                        "cannot inspect path %r: %s"
                        % (original_path, exc.strerror),
                        errno=exc.errno,
                        path=original_path,
                    ) from exc

                if stat.S_ISLNK(st.st_mode):
                    raise PathEscapeError(
                        "path_escape",
                        "path contains a symbolic link: %r" % original_path,
                        path=original_path,
                    )
                if st.st_dev != root_dev:
                    raise PathEscapeError(
                        "path_escape",
                        "path crosses a mount point: %r" % original_path,
                        path=original_path,
                    )
                if index < len(components) - 1:
                    if not stat.S_ISDIR(st.st_mode):
                        raise PathEscapeError(
                            "path_escape",
                            "path component is not a directory: %r" % original_path,
                            path=original_path,
                        )
                    try:
                        next_fd = os.open(part, _DIR_OPEN_FLAGS, dir_fd=current)
                    except OSError as exc:
                        raise PathEscapeError(
                            "path_escape",
                            "path contains a symbolic link: %r" % original_path,
                            path=original_path,
                        ) from exc
                    if current != root_fd:
                        os.close(current)
                    current = next_fd
        finally:
            if current != root_fd:
                os.close(current)
            os.close(root_fd)

    def resolve(self, root, relative, symlinks=None):
        root_text = _as_text(root)
        relative_text = _as_text(relative)
        components = self._components(relative_text, allow_root=True)
        components = _resolve_static_symlinks(
            root_text, components, symlinks, relative_text
        )

        joined = "/".join(components)
        if len(joined) > self.max_path_length:
            raise PathEscapeError(
                "path_escape",
                "path is too long after normalization: %r" % relative_text,
                path=relative_text,
            )
        if _is_magic_link_path(components):
            raise PathEscapeError(
                "path_escape",
                "magic process file-descriptor links are not permitted: %r"
                % relative_text,
                path=relative_text,
            )

        self._verify_existing_path(root_text, components, relative_text)
        if not components:
            return root_text
        return os.path.join(root_text, *components)

    def write_file(self, root, relative, data, fail_after_temp=False):
        root_text = _as_text(root)
        relative_text = _as_text(relative)
        components = self._components(relative_text)
        result_path = os.path.join(root_text, *components)

        root_fd = self._open_root(root_text)
        try:
            root_is_symlink = stat.S_ISLNK(
                os.stat(root_text, follow_symlinks=False).st_mode
            )
        except OSError:
            os.close(root_fd)
            raise
        created = []
        parent_fd = None
        file_fd = None
        temp_name = None
        committed = False
        try:
            root_dev = os.fstat(root_fd).st_dev
            parent_fd, created = self._walk_directories(
                root_fd,
                root_dev,
                components[:-1],
                create=True,
                path=relative_text,
            )
            final_name = components[-1]

            try:
                st = os.stat(final_name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                st = None
            except OSError as exc:
                if exc.errno in (_errno.ELOOP, _errno.ENOTDIR):
                    raise PathEscapeError(
                        "path_escape",
                        "target was replaced by a symlink: %r" % relative_text,
                        path=relative_text,
                    ) from exc
                raise

            if st is not None:
                if stat.S_ISLNK(st.st_mode):
                    raise PathEscapeError(
                        "path_escape",
                        "target is a symbolic link: %r" % relative_text,
                        path=relative_text,
                    )
                if stat.S_ISDIR(st.st_mode):
                    raise PathStorageError(
                        "target is a directory: %r" % relative_text,
                        errno=_errno.EISDIR,
                        path=relative_text,
                    )
                if st.st_dev != root_dev:
                    raise PathEscapeError(
                        "path_escape",
                        "target crosses a mount point: %r" % relative_text,
                        path=relative_text,
                    )

            while True:
                temp_name = ".warden-tmp-%s-%d" % (
                    secrets.token_hex(16),
                    next(_UNIQUE_COUNTER),
                )
                try:
                    file_fd = os.open(
                        temp_name,
                        _FILE_OPEN_FLAGS,
                        0o600,
                        dir_fd=parent_fd,
                    )
                    break
                except FileExistsError:
                    continue

            view = memoryview(data)
            while view:
                written = os.write(file_fd, view)
                if written <= 0:
                    raise OSError(_errno.EIO, "short write")
                view = view[written:]
            os.fsync(file_fd)

            if fail_after_temp:
                raise _FailAfterTemp()

            self._verify_directory_identity(
                root_text,
                root_fd,
                parent_fd,
                components[:-1],
                relative_text,
                root_is_symlink,
            )
            os.replace(
                temp_name,
                final_name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            committed = True
            temp_name = None
            try:
                os.fsync(parent_fd)
            except OSError:
                pass
        except _FailAfterTemp:
            committed = False
        except PathConfinementError:
            raise
        except OSError as exc:
            raise PathStorageError(
                "failed to write %r: %s"
                % (relative_text, exc.strerror or str(exc)),
                errno=exc.errno,
                path=relative_text,
            ) from exc
        finally:
            if file_fd is not None:
                try:
                    os.close(file_fd)
                except OSError:
                    pass
            if temp_name is not None:
                try:
                    os.unlink(temp_name, dir_fd=parent_fd)
                except OSError:
                    pass
            if parent_fd is not None:
                os.close(parent_fd)
            if committed:
                self._close_created_trackers(created)
            else:
                self._discard_created_directories(created)
            os.close(root_fd)

        return {
            "committed": committed,
            "path": result_path,
            "temp_cleaned": True,
        }

    def extract(self, root, entries):
        """Extract all entries after validating every destination first.

        Validation is intentionally a separate pass.  A malformed entry near
        the end of a large archive therefore cannot leave files from earlier
        entries behind.
        """

        root_text = _as_text(root)
        stored = []
        directory_names = {}
        file_names = {}
        for entry in entries:
            if not isinstance(entry, (tuple, list)) or len(entry) != 2:
                raise PathConfinementError(
                    "invalid_entry", "archive entry must be a (path, data) pair"
                )
            relative, data = entry
            relative_text = _as_text(relative)
            components = self._components(relative_text)
            self.resolve(root_text, relative_text)

            folded = tuple(part.casefold() for part in components)
            for index in range(1, len(components)):
                prefix = folded[:index]
                actual_prefix = components[:index]
                if prefix in file_names:
                    raise PathConflictError(
                        "path_conflict",
                        "archive entry uses a file as a directory: %r"
                        % relative_text,
                        path=relative_text,
                    )
                previous = directory_names.get(prefix)
                if previous is not None and previous != actual_prefix:
                    raise PathConflictError(
                        "path_conflict",
                        "archive directory names differ only by case or "
                        "normalization: %r and %r"
                        % (previous, actual_prefix),
                        path=relative_text,
                    )
                directory_names[prefix] = actual_prefix

            previous = directory_names.get(folded)
            if previous is not None and previous != components:
                raise PathConflictError(
                    "path_conflict",
                    "archive path is both a file and a directory: %r"
                    % relative_text,
                    path=relative_text,
                )
            previous = file_names.get(folded)
            if previous is not None and previous != components:
                raise PathConflictError(
                    "path_conflict",
                    "archive entries differ only by case or normalization: %r "
                    "and %r" % (previous, components),
                    path=relative_text,
                )
            file_names[folded] = components
            stored.append((relative_text, components, data))

        if not stored:
            return []

        root_fd = self._open_root(root_text)
        root_dev = os.fstat(root_fd).st_dev
        stage_name = None
        stage_fd = None
        created_paths = []
        committed = []
        try:
            root_is_symlink = stat.S_ISLNK(
                os.stat(root_text, follow_symlinks=False).st_mode
            )
            stage_name, stage_fd = self._create_stage(
                root_fd, root_dev, root_text
            )

            for index, (relative_text, components, data) in enumerate(stored):
                staged_name = "file-%08d" % index
                try:
                    self._write_stage_file(stage_fd, staged_name, data)
                except OSError as exc:
                    raise PathStorageError(
                        "failed to stage %r: %s"
                        % (relative_text, exc.strerror or str(exc)),
                        errno=exc.errno,
                        path=relative_text,
                    ) from exc

            results = []
            for index, (relative_text, components, _data) in enumerate(stored):
                staged_name = "file-%08d" % index
                parent_fd = None
                created = []
                try:
                    parent_fd, created = self._walk_directories(
                        root_fd,
                        root_dev,
                        components[:-1],
                        create=True,
                        path=relative_text,
                        created_paths=created_paths,
                    )
                    final_name = components[-1]
                    try:
                        st = os.stat(
                            final_name,
                            dir_fd=parent_fd,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        st = None
                    except OSError as exc:
                        if exc.errno in (_errno.ELOOP, _errno.ENOTDIR):
                            raise PathEscapeError(
                                "path_escape",
                                "target was replaced by a symlink: %r"
                                % relative_text,
                                path=relative_text,
                            ) from exc
                        raise

                    if st is not None:
                        if stat.S_ISLNK(st.st_mode):
                            raise PathEscapeError(
                                "path_escape",
                                "target is a symbolic link: %r" % relative_text,
                                path=relative_text,
                            )
                        if stat.S_ISDIR(st.st_mode):
                            raise PathStorageError(
                                "target is a directory: %r" % relative_text,
                                errno=_errno.EISDIR,
                                path=relative_text,
                            )
                        if not stat.S_ISREG(st.st_mode):
                            raise PathStorageError(
                                "target is not a regular file: %r"
                                % relative_text,
                                errno=_errno.EINVAL,
                                path=relative_text,
                            )
                        if st.st_dev != root_dev:
                            raise PathEscapeError(
                                "path_escape",
                                "target crosses a mount point: %r"
                                % relative_text,
                                path=relative_text,
                            )

                    backup_name = None
                    if st is not None:
                        backup_name = "backup-%08d" % index
                        os.link(
                            final_name,
                            backup_name,
                            src_dir_fd=parent_fd,
                            dst_dir_fd=stage_fd,
                            follow_symlinks=False,
                        )

                    self._verify_directory_identity(
                        root_text,
                        root_fd,
                        parent_fd,
                        components[:-1],
                        relative_text,
                        root_is_symlink,
                    )
                    os.replace(
                        staged_name,
                        final_name,
                        src_dir_fd=stage_fd,
                        dst_dir_fd=parent_fd,
                    )
                    committed.append(
                        (relative_text, components, st is not None, backup_name)
                    )
                    results.append(
                        {
                            "committed": True,
                            "path": os.path.join(root_text, *components),
                            "temp_cleaned": True,
                        }
                    )
                finally:
                    if parent_fd is not None:
                        os.close(parent_fd)
                    self._close_created_trackers(created)
        except PathConfinementError:
            self._rollback_extract(
                root_fd,
                root_text,
                root_dev,
                stage_fd,
                committed,
                created_paths,
            )
            raise
        except OSError as exc:
            self._rollback_extract(
                root_fd,
                root_text,
                root_dev,
                stage_fd,
                committed,
                created_paths,
            )
            raise PathStorageError(
                "extraction failed: %s" % (exc.strerror or str(exc)),
                errno=exc.errno,
                path=root_text,
            ) from exc
        finally:
            self._remove_stage(root_fd, stage_name, stage_fd)
            os.close(root_fd)

        return results

    def _rollback_extract(
        self, root_fd, root_text, root_dev, stage_fd, committed, created_paths
    ):
        for relative_text, components, existed, backup_name in reversed(committed):
            parent_fd = None
            try:
                parent_fd, _ = self._walk_directories(
                    root_fd,
                    root_dev,
                    components[:-1],
                    create=False,
                    path=relative_text,
                )
                final_name = components[-1]
                if existed and backup_name is not None:
                    os.replace(
                        backup_name,
                        final_name,
                        src_dir_fd=stage_fd,
                        dst_dir_fd=parent_fd,
                    )
                else:
                    try:
                        os.unlink(final_name, dir_fd=parent_fd)
                    except FileNotFoundError:
                        pass
            except (OSError, PathConfinementError):
                pass
            finally:
                if parent_fd is not None:
                    os.close(parent_fd)

        self._remove_empty_created_directories(
            root_fd, root_dev, created_paths, root_text
        )
