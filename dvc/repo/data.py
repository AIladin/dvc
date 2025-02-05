import os
import posixpath
from itertools import chain
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    Iterable,
    Iterator,
    List,
    Optional,
    TypedDict,
    Union,
)

if TYPE_CHECKING:
    from dvc.repo import Repo
    from dvc.scm import Git, NoSCM
    from dvc_data.index import DataIndex


def posixpath_to_os_path(path: str) -> str:
    return path.replace(posixpath.sep, os.path.sep)


def _adapt_typ(typ):
    from dvc_data.index.diff import ADD, DELETE, MODIFY

    if typ == MODIFY:
        return "modified"

    if typ == ADD:
        return "added"

    if typ == DELETE:
        return "deleted"

    return typ


def _adapt_path(change):
    isdir = False
    if change.new and change.new.meta:
        isdir = change.new.meta.isdir
    elif change.old and change.old.meta:
        isdir = change.old.meta.isdir
    key = change.key
    if isdir:
        key = (*key, "")
    return os.path.sep.join(key)


def _diff(
    old: "DataIndex",
    new: "DataIndex",
    *,
    granular: Optional[bool] = False,
    with_missing: Optional[bool] = False,
) -> Dict[str, List[str]]:
    from dvc_data.index.diff import UNCHANGED, UNKNOWN, diff

    ret: Dict[str, List[str]] = {}

    def _add_change(typ, change):
        typ = _adapt_typ(typ)
        if typ not in ret:
            ret[typ] = []

        ret[typ].append(_adapt_path(change))

    for change in diff(
        old,
        new,
        with_unchanged=True,
        shallow=not granular,
        hash_only=True,
        with_unknown=True,
    ):
        if (
            change.typ == UNCHANGED
            and change.old
            and change.new
            and not change.old.hash_info
            and not change.new.hash_info
        ):
            # NOTE: emulating previous behaviour
            continue

        if change.typ == UNKNOWN and not change.new:
            # NOTE: emulating previous behaviour
            continue

        if (
            with_missing
            and change.old
            and change.old.hash_info
            and not change.old.odb.exists(change.old.hash_info.value)
        ):
            # NOTE: emulating previous behaviour
            _add_change("not_in_cache", change)

        _add_change(change.typ, change)

    return ret


class GitInfo(TypedDict, total=False):
    staged: Dict[str, List[str]]
    unstaged: Dict[str, List[str]]
    untracked: List[str]
    is_empty: bool
    is_dirty: bool


def _git_info(
    scm: Union["Git", "NoSCM"], untracked_files: str = "all"
) -> GitInfo:
    from scmrepo.exceptions import SCMError

    from dvc.scm import NoSCM

    if isinstance(scm, NoSCM):
        return {}

    try:
        scm.get_rev()
    except SCMError:
        empty_repo = True
    else:
        empty_repo = False

    staged, unstaged, untracked = scm.status(untracked_files=untracked_files)
    if os.name == "nt":
        untracked = [posixpath_to_os_path(path) for path in untracked]
    # NOTE: order is important here.
    return GitInfo(
        staged=staged,
        unstaged=unstaged,
        untracked=untracked,
        is_empty=empty_repo,
        is_dirty=any([staged, unstaged, untracked]),
    )


def _diff_index_to_wtree(repo: "Repo", **kwargs: Any) -> Dict[str, List[str]]:
    from .index import build_data_index

    workspace = build_data_index(
        repo.index, repo.root_dir, repo.fs, compute_hash=True
    )

    return _diff(
        repo.index.data["repo"],
        workspace,
        with_missing=True,
        **kwargs,
    )


def _diff_head_to_index(
    repo: "Repo", head: str = "HEAD", **kwargs: Any
) -> Dict[str, List[str]]:
    index = repo.index.data["repo"]

    for rev in repo.brancher(revs=[head]):
        if rev == "workspace":
            continue

        head_index = repo.index.data["repo"]

    return _diff(head_index, index, **kwargs)


class Status(TypedDict):
    not_in_cache: List[str]
    committed: Dict[str, List[str]]
    uncommitted: Dict[str, List[str]]
    untracked: List[str]
    unchanged: List[str]
    git: GitInfo


def _transform_git_paths_to_dvc(
    repo: "Repo", files: Iterable[str]
) -> List[str]:
    """Transform files rel. to Git root to DVC root, and drop outside files."""
    rel = repo.fs.path.relpath(repo.root_dir, repo.scm.root_dir).rstrip("/")

    # if we have repo root in a different location than scm's root,
    # i.e. subdir repo, all git_paths need to be transformed rel. to the DVC
    # repo root and anything outside need to be filtered out.
    if rel not in (os.curdir, ""):
        prefix = rel + os.sep
        length = len(prefix)
        files = (file[length:] for file in files if file.startswith(prefix))

    start = repo.fs.path.relpath(repo.fs.path.getcwd(), repo.root_dir)
    if start in (os.curdir, ""):
        return list(files)
    # we need to convert repo relative paths to curdir relative.
    return [repo.fs.path.relpath(file, start) for file in files]


def status(repo: "Repo", untracked_files: str = "no", **kwargs: Any) -> Status:
    from dvc.scm import NoSCMError, SCMError

    head = kwargs.pop("head", "HEAD")
    uncommitted_diff = _diff_index_to_wtree(repo, **kwargs)
    not_in_cache = uncommitted_diff.pop("not_in_cache", [])
    unchanged = set(uncommitted_diff.pop("unchanged", []))

    try:
        committed_diff = _diff_head_to_index(repo, head=head, **kwargs)
    except (SCMError, NoSCMError):
        committed_diff = {}
    else:
        unchanged &= set(committed_diff.pop("unchanged", []))

    git_info = _git_info(repo.scm, untracked_files=untracked_files)
    untracked = git_info.get("untracked", [])
    untracked = _transform_git_paths_to_dvc(repo, untracked)
    # order matters here
    return Status(
        not_in_cache=not_in_cache,
        committed=committed_diff,
        uncommitted=uncommitted_diff,
        untracked=untracked,
        unchanged=list(unchanged),
        git=git_info,
    )


def ls(
    repo: "Repo",
    targets: Optional[List[Optional[str]]] = None,
    recursive: bool = False,
) -> Iterator[Dict[str, Any]]:
    targets = targets or [None]
    pairs = chain.from_iterable(
        repo.stage.collect_granular(target, recursive=recursive)
        for target in targets
    )
    for stage, filter_info in pairs:
        for out in stage.filter_outs(filter_info):
            yield {"path": str(out), **out.annot.to_dict()}
