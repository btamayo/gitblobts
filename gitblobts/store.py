import calendar
import dataclasses
import importlib
import inspect
import itertools
import logging
import math
import pathlib
import time
import typing
from typing import Any, Iterable, Iterator, List, Optional, Union

import cryptography.fernet
import dateparser
import git

import gitblobts.exc as exc

log = logging.getLogger(__name__)

Timestamp = Union[None, float, time.struct_time, str]


@dataclasses.dataclass
class Blob:
    time_utc_ns: int
    blob: bytes


def generate_key() -> bytes:
    return cryptography.fernet.Fernet.generate_key()


class Store:

    def __init__(self, path: Union[str, pathlib.Path], *, compression: Optional[str] = None,
                 key: Optional[bytes] = None):
        self._path = pathlib.Path(path)
        self._compression = importlib.import_module(compression) if compression else None  # e.g. bz2, gzip, lzma
        self._encryption = cryptography.fernet.Fernet(key) if key else None
        self._repo = git.Repo(self._path)  # Can raise git.exc.NoSuchPathError or git.exc.InvalidGitRepositoryError.
        self._log_state()
        self._check_repo()

    def _addblob(self, blob: bytes, time_utc: Union[None, Timestamp], *, push: bool) -> int:
        push_state = 'with' if push else 'without'
        log.info('Adding blob of length %s and time "%s" %s repository push.', len(blob), time_utc, push_state)
        if not isinstance(blob, bytes):
            raise exc.BlobTypeInvalid('Blob must be an instance of type bytes, but it is of '
                                      f'type {type(blob).__qualname__}.')

        repo = self._repo
        time_utc_ns = self._standardize_time_to_ns(time_utc)

        # Note: Zero left-padding of the filename is intentionally not used as it can lead to comparison errors.
        while True:  # Use filename that doesn't already exist. Avoid overwriting existing file.
            path = self._path / str(time_utc_ns)
            if path.exists():
                time_utc_ns += 1
            else:
                break

        blob_original = blob
        blob = self._process_in(blob)
        log.debug('Writing %s bytes to file %s.', len(blob), path.name)
        path.write_bytes(blob)
        log.info('Wrote %s bytes to file %s.', len(blob), path.name)

        repo.index.add([str(path)])
        log.info('Added file %s to repository index.', path.name)
        if push:
            self._commit_and_push_repo()
        assert blob_original == self._process_out(path.read_bytes())
        log.info('Added blob of raw length %s and processed length %s with name %s.', len(blob_original), len(blob),
                 path.name)
        return time_utc_ns

    def _check_repo(self) -> None:
        repo = self._repo
        log.debug('Checking repository.')
        if repo.bare:  # This is not implicit.
            raise exc.RepoBare('Repository must not be bare.')
        # if repo.active_branch.name != 'master':
        #     raise exc.RepoBranchNotMaster('Active repository branch must be "master".')
        log.info('Active repository branch is "%s".', repo.active_branch.name)
        if repo.is_dirty():
            raise exc.RepoDirty('Repository must not be dirty.')
        if repo.untracked_files:
            names = '\n'.join(repo.untracked_files)
            raise exc.RepoHasUntrackedFiles(f'Repository must not have any untracked files. It has these:\n{names}')
        if not repo.remotes:
            raise exc.RepoRemoteNotAdded('Repository must have a remote.')
        if not repo.remote().exists():
            raise exc.RepoRemoteNotExist('Repository remote must exist.')
        # if not repo.remote().name == 'origin':
        #     raise exc.RemoteRepoError('Repository remote name must be "origin".')
        log.info('Repository remote is "%s".', repo.remote().name)
        log.info('Finished checking repository.')

    def _commit_and_push_repo(self) -> None:
        repo = self._repo
        # Note: repo.index.entries was observed to also include unpushed files in addition to uncommitted files.
        log.debug('Committing repository index.')
        self._repo.index.commit('')
        log.info('Committed repository index.')

        def _is_pushed(push_info: git.remote.PushInfo) -> bool:
            return push_info.flags == push_info.FAST_FORWARD  # This check can require the use of & instead.

        remote = repo.remote()
        log.debug('Pushing to repository remote "%s".', remote.name)
        push_info = remote.push()[0]
        is_pushed = _is_pushed(push_info)
        logger = log.debug if is_pushed else log.warning
        logger('Push flags were %s and message was "%s".', push_info.flags, push_info.summary.strip())
        if not is_pushed:
            log.warning('Failed first attempt at pushing to repository remote "%s". A pull will be performed.',
                        remote.name)
            self._pull_repo()
            log.info('Reattempting to push to repository remote "%s".', remote.name)
            push_info = remote.push()[0]
            is_pushed = _is_pushed(push_info)
            logger = log.debug if is_pushed else log.error
            logger('Push flags were %s and message was "%s".', push_info.flags, push_info.summary.strip())
            if not is_pushed:
                raise exc.RepoPushError(f'Failed to push to repository remote "{remote.name}" despite a pull.')
        log.info('Pushed to repository remote "%s".', remote.name)

    def _compress(self, blob: bytes) -> bytes:
        log.debug('Compressing blob.') if self._compression else log.debug('Skipping blob compression.')
        return self._compression.compress(blob) if self._compression else blob

    def _decompress(self, blob: bytes) -> bytes:
        log.debug('Decompressing blob.') if self._compression else log.debug('Skipping blob decompression.')
        return self._compression.decompress(blob) if self._compression else blob

    def _decrypt(self, blob: bytes) -> bytes:
        log.debug('Decrypting blob.') if self._encryption else log.debug('Skipping blob decryption.')
        return self._encryption.decrypt(blob) if self._encryption else blob

    def _encrypt(self, blob: bytes) -> bytes:
        log.debug('Encrypting blob.') if self._encryption else log.debug('Skipping blob encryption.')
        return self._encryption.encrypt(blob) if self._encryption else blob

    def _log_state(self) -> None:
        log.info('Repository path is "%s".', self._path)
        log.info('Compression is %s.',
                 f'enabled with {self._compression.__name__}' if self._compression else 'not enabled')
        log.info('Encryption is %s.',
                 f'enabled with {self._encryption.__class__.__name__}' if self._encryption else 'not enabled')

    def _process_in(self, blob: bytes) -> bytes:
        return self._encrypt(self._compress(blob))

    def _process_out(self, blob: bytes) -> bytes:
        return self._decompress(self._decrypt(blob))

    def _pull_repo(self) -> None:
        remote = self._repo.remote()
        name = remote.name

        def _is_pulled(pull_info: git.remote.FetchInfo) -> bool:
            valid_flags = {pull_info.HEAD_UPTODATE, pull_info.FAST_FORWARD}
            return pull_info.flags in valid_flags  # This check can require the use of & instead.

        log.debug('Pulling from repository remote "%s".', name)
        try:
            pull_info = remote.pull()[0]
        except git.exc.GitCommandError:  # Could be due to no push ever.
            log.warning('Failed to pull from repository remote "%s".', name)
        else:
            is_pulled = _is_pulled(pull_info)
            logger = log.debug if is_pulled else log.error
            logger('Pull flags were %s.', pull_info.flags)
            if not is_pulled:
                raise exc.RepoPullError(f'Failed to pull from repository remote "{remote.name}".')
            log.info('Pulled from repository remote "%s".', name)

    def _standardize_time_to_ns(self, time_utc: Timestamp) -> int:
        def _convert_seconds_to_positive_ns(seconds: Union[int, float]) -> int:
            nanoseconds = int(round(seconds * int(1e9)))
            return max(1, nanoseconds)

        if time_utc is None:
            return time.time_ns()
        elif time_utc == 0:  # OK as int since 0 seconds is 0 nanoseconds.
            return 0
        elif isinstance(time_utc, float):
            if not math.isfinite(time_utc):
                raise exc.TimeInvalid(f'Provided time "{time_utc}" must be finite and not NaN for use as a filename.')
            elif time_utc < 0:
                raise exc.TimeInvalid(f'Provided time "{time_utc}" must be non-negative for use as a filename.')
            return _convert_seconds_to_positive_ns(time_utc)
        elif isinstance(time_utc, time.struct_time):
            if time_utc.tm_zone == 'GMT':
                time_utc = calendar.timegm(time_utc)
            else:
                time_utc = time.mktime(time_utc)
            return _convert_seconds_to_positive_ns(time_utc)
        elif isinstance(time_utc, str):
            time_utc_input = time_utc
            time_utc = dateparser.parse(time_utc, settings={'TO_TIMEZONE': 'UTC', 'RETURN_AS_TIMEZONE_AWARE': True,
                                                            'PREFER_DATES_FROM': 'past'})
            if time_utc is None:
                raise exc.TimeInvalid(f'Provided time "{time_utc_input}" could not be parsed. It must be parsable by '
                                      'dateparser.')
            return _convert_seconds_to_positive_ns(time_utc.timestamp())
        else:
            annotation = typing.get_type_hints(self._standardize_time_to_ns)['time_utc']
            raise exc.TimeUnhandledType(f'Provided time "{time_utc}" is of an unhandled type "{type(time_utc)}. '
                                        f'It must be conform to {annotation}.')

    def addblob(self, blob: bytes, time_utc: Optional[Timestamp] = None) -> int:
        return self._addblob(blob, time_utc, push=True)

    def addblobs(self, blobs: Iterable[bytes], times_utc: Optional[Iterable[Timestamp]] = None) -> List[int]:
        log.info('Adding blobs.')
        if times_utc is None:
            times_utc = []
        times_utc_ns = [self._addblob(blob, time_utc, push=False) for blob, time_utc in
                        itertools.zip_longest(blobs, times_utc)]
        self._commit_and_push_repo()
        log.info('Added %s blobs.', len(times_utc_ns))
        return times_utc_ns

    def getblobs(self, start_utc: Optional[Timestamp] = 0., end_utc: Optional[Timestamp] = math.inf,
                 *, pull: Optional[bool] = False) -> Iterator[Blob]:
        pull_state = 'with' if pull else 'without'
        log.debug('Getting blobs from "%s" to "%s" UTC %s repository pull.', start_utc, end_utc, pull_state)

        def standardize_time_to_ns(time_utc):
            try:
                if time_utc < 0:
                    return 0  # This is lowest possible filename of timestamp.
            except TypeError:
                pass
            if time_utc == math.inf:
                return time_utc
            return self._standardize_time_to_ns(time_utc)

        # Note: Either one of start_utc and end_utc can rightfully be smaller.
        def default_value(arg: str) -> Any:
            return inspect.signature(self.getblobs).parameters[arg].default
        start_utc = standardize_time_to_ns(start_utc) if start_utc is not None else default_value('start_utc')
        end_utc = standardize_time_to_ns(end_utc) if end_utc is not None else default_value('end_utc')
        log.info('Getting blobs from %s to %s UTC %s repository pull.', start_utc, end_utc, pull_state)

        if start_utc == end_utc:
            log.warning('The effective start and end times are the same. As such, 0 or 1 blobs will be yielded.')
        elif set([start_utc, end_utc]) == set([0, math.inf]):  # This is a careful check of full range.
            log.warning('The time range is infinity. As such, all blobs will be yielded.')

        if pull:
            self._pull_repo()
        paths = (path for path in self._path.iterdir() if path.is_file())
        if start_utc <= end_utc:
            order = 'ascending'
            times_utc_ns = (int(path.name) for path in paths if start_utc <= int(path.name) <= end_utc)
            times_utc_ns = sorted(times_utc_ns)
        else:
            order = 'descending'
            times_utc_ns = (int(path.name) for path in paths if end_utc <= int(path.name) <= start_utc)
            times_utc_ns = sorted(times_utc_ns, reverse=True)
        if times_utc_ns:
            log.debug('Yielding up to %s blobs in %s chronological order.', len(times_utc_ns), order)

        for time_utc_ns in times_utc_ns:
            path = self._path / str(time_utc_ns)
            log.debug('Yielding blob %s.', path.name)
            yield Blob(time_utc_ns, self._process_out(path.read_bytes()))
            log.info('Yielded blob %s.', path.name)
        log.info('Yielded %s blobs.', len(times_utc_ns))
