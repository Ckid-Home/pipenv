import copy
import itertools
import os
import stat
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from json import JSONDecodeError
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

from pipenv.patched.pip._internal.req.req_install import InstallRequirement
from pipenv.utils.constants import VCS_LIST
from pipenv.utils.dependencies import (
    clean_resolved_dep,
    determine_vcs_revision_hash,
    expansive_install_req_from_line,
    normalize_vcs_url,
    pep423_name,
    translate_markers,
)
from pipenv.utils.exceptions import (
    LockfileCorruptException,
    MissingParameter,
    PipfileNotFound,
)
from pipenv.utils.pipfile import DEFAULT_NEWLINES, ProjectFile
from pipenv.utils.requirements import normalize_name
from pipenv.utils.requirementslib import is_editable, is_vcs, merge_items
from pipenv.vendor.plette import lockfiles


def merge_markers(entry, markers):
    if not isinstance(markers, list):
        markers = [markers]
    for marker in markers:
        if not isinstance(marker, str):
            marker = str(marker)
        if "markers" not in entry:
            entry["markers"] = marker
        elif marker not in entry["markers"]:
            entry["markers"] = f"({entry['markers']}) and ({marker})"


def format_requirement_for_lockfile(
    req: InstallRequirement,
    markers_lookup: Dict[str, str],
    index_lookup: Dict[str, str],
    original_deps: Dict[str, Any],
    pipfile_entries: Dict[str, Any],
    hashes: Optional[Set[str]] = None,
) -> Tuple[str, Dict[str, Any]]:
    """Format a requirement for the lockfile with improved VCS handling."""
    name = normalize_name(req.name)
    entry: Dict[str, Any] = {"name": name}
    pipfile_entry = pipfile_entries.get(name, pipfile_entries.get(req.name, {}))
    # Handle VCS requirements
    is_vcs_dep = next(iter([vcs for vcs in VCS_LIST if vcs in pipfile_entry]), None)
    if req.link and req.link.is_vcs:
        is_vcs_dep = True
    if is_vcs_dep:
        if req.link and req.link.is_vcs:
            link = req.link
        else:
            link = req.cached_wheel_source_link
        vcs = link.scheme.split("+", 1)[0]

        # Get VCS URL from original deps or normalize the link URL
        vcs_url, _ = normalize_vcs_url(link.url)
        entry[vcs] = vcs_url

        # Handle subdirectory information
        if pipfile_entry.get("subdirectory"):
            entry["subdirectory"] = pipfile_entry["subdirectory"]
        elif link.subdirectory_fragment:
            entry["subdirectory"] = link.subdirectory_fragment

        # Handle reference information - try multiple sources
        ref = determine_vcs_revision_hash(req, vcs, pipfile_entry.get("ref"))
        if ref:
            entry["ref"] = ref
    # Handle non-VCS requirements
    else:
        if req.req and req.req.specifier:
            entry["version"] = str(req.req.specifier)
        elif req.specifier:
            entry["version"] = str(req.specifier)
        if req.link and req.link.is_file:
            entry["file"] = req.link.url
    # Add index information
    if name in index_lookup:
        entry["index"] = index_lookup[name]

    # Handle markers
    markers = req.markers
    if markers:
        entry["markers"] = str(markers)
    if name in markers_lookup:
        merge_markers(entry, markers_lookup[name])
    if isinstance(pipfile_entry, dict):
        if "markers" in pipfile_entry:
            merge_markers(entry, pipfile_entry["markers"])
        if "os_name" in pipfile_entry:
            merge_markers(entry, f"os_name {pipfile_entry['os_name']}")

    # Handle extras
    if req.extras:
        entry["extras"] = sorted(req.extras)

    # Handle hashes
    if hashes:
        entry["hashes"] = sorted(set(hashes))

    # Handle file/path entries from Pipfile
    if isinstance(pipfile_entry, dict):
        if pipfile_entry.get("file"):
            entry["file"] = pipfile_entry["file"]
            if pipfile_entry.get("editable"):
                entry["editable"] = pipfile_entry["editable"]
            entry.pop("version", None)
            entry.pop("index", None)
        elif pipfile_entry.get("path"):
            entry["path"] = pipfile_entry["path"]
            if pipfile_entry.get("editable"):
                entry["editable"] = pipfile_entry["editable"]
            entry.pop("version", None)
            entry.pop("index", None)

    entry = translate_markers(entry)
    return name, entry


def get_locked_dep(project, dep, pipfile_section, current_entry=None):
    # initialize default values
    is_top_level = False

    # if the dependency has a name, find corresponding entry in pipfile
    if isinstance(dep, dict) and dep.get("name"):
        dep_name = pep423_name(dep["name"])
        for pipfile_key, pipfile_entry in pipfile_section.items():
            if pep423_name(pipfile_key) == dep_name or pipfile_key == dep_name:
                is_top_level = True
                if isinstance(pipfile_entry, dict):
                    if pipfile_entry.get("version"):
                        pipfile_entry.pop("version")
                    if pipfile_entry.get("ref"):
                        pipfile_entry.pop("ref")
                    dep.update(pipfile_entry)
                break

    # clean the dependency
    lockfile_entry = clean_resolved_dep(project, dep, is_top_level, current_entry)

    # get the lockfile version and compare with pipfile version
    lockfile_name, lockfile_dict = lockfile_entry.copy().popitem()
    lockfile_entry[lockfile_name] = lockfile_dict

    return lockfile_entry


def prepare_lockfile(project, results, pipfile, lockfile_section, old_lock_data=None):
    for dep in results:
        if not dep:
            continue
        dep_name = dep["name"]
        current_entry = None
        if dep_name in old_lock_data:
            current_entry = old_lock_data[dep_name]
        lockfile_entry = get_locked_dep(project, dep, pipfile, current_entry)

        # If the current dependency doesn't exist in the lockfile, add it
        if dep_name not in lockfile_section:
            lockfile_section[dep_name] = lockfile_entry[dep_name]
        else:
            # If the dependency exists, update the details
            current_entry = lockfile_section[dep_name]
            if not isinstance(current_entry, dict):
                lockfile_section[dep_name] = lockfile_entry[dep_name]
            else:
                # If the current entry is a dict, merge the new details
                lockfile_section[dep_name].update(lockfile_entry[dep_name])
                lockfile_section[dep_name] = translate_markers(lockfile_section[dep_name])
    return lockfile_section


@contextmanager
def atomic_open_for_write(target, binary=False, newline=None, encoding=None) -> None:
    """Atomically open `target` for writing.
    This is based on Lektor's `atomic_open()` utility, but simplified a lot
    to handle only writing, and skip many multiprocess/thread edge cases
    handled by Werkzeug.
    :param target: Target filename to write (string or Path)
    :param bool binary: Whether to open in binary mode, default False
    :param Optional[str] newline: The newline character to use when writing, determined
        from system if not supplied.
    :param Optional[str] encoding: The encoding to use when writing, defaults to system
        encoding.
    How this works:
    * Create a temp file (in the same directory of the actual target), and
      yield for surrounding code to write to it.
    * If something goes wrong, try to remove the temp file. The actual target
      is not touched whatsoever.
    * If everything goes well, close the temp file, and replace the actual
      target with this new file.
    . code:: python
        >>> fn = "test_file.txt"
        >>> def read_test_file(filename=fn):
                with open(filename, 'r') as fh:
                    print(fh.read().strip())
        >>> with open(fn, "w") as fh:
                fh.write("this is some test text")
        >>> read_test_file()
        this is some test text
        >>> def raise_exception_while_writing(filename):
                with open(filename, "w") as fh:
                    fh.write("writing some new text")
                    raise RuntimeError("Uh oh, hope your file didn't get overwritten")
        >>> raise_exception_while_writing(fn)
        Traceback (most recent call last):
            ...
        RuntimeError: Uh oh, hope your file didn't get overwritten
        >>> read_test_file()
        writing some new text
        >>> def raise_exception_while_writing(filename):
                with atomic_open_for_write(filename) as fh:
                    fh.write("Overwriting all the text from before with even newer text")
                    raise RuntimeError("But did it get overwritten now?")
        >>> raise_exception_while_writing(fn)
            Traceback (most recent call last):
                ...
            RuntimeError: But did it get overwritten now?
        >>> read_test_file()
            writing some new text
    """
    # Convert target to Path object
    target_path = Path(target)

    # Create mode string
    mode = "w+b" if binary else "w"

    # Create temporary file in the same directory as the target
    f = NamedTemporaryFile(
        dir=str(target_path.parent),
        prefix=".__atomic-write",
        mode=mode,
        encoding=encoding,
        newline=newline,
        delete=False,
    )

    # Get path object for the temporary file
    temp_path = Path(f.name)

    # Set permissions to 0644
    with suppress(OSError):
        temp_path.chmod(stat.S_IWUSR | stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)

    try:
        yield f
    except BaseException:
        f.close()
        with suppress(OSError):
            temp_path.unlink(missing_ok=True)
        raise
    else:
        f.close()
        try:
            # This is needed on Windows
            target_path.unlink(missing_ok=True)
        except OSError:
            pass

        # Rename the temporary file to the target
        # Note: Path.rename() is equivalent to os.rename()
        temp_path.rename(target_path)


@dataclass
class Lockfile:
    lockfile: lockfiles.Lockfile
    path: Path = field(
        default_factory=lambda: Path(os.curdir).joinpath("Pipfile.lock").absolute()
    )
    _requirements: Optional[List[Any]] = field(default_factory=list)
    _dev_requirements: Optional[List[Any]] = field(default_factory=list)
    projectfile: ProjectFile = None
    newlines: str = DEFAULT_NEWLINES

    def __post_init__(self):
        if not self.path:
            self.path = Path(os.curdir).absolute()
        if not self.projectfile:
            self.projectfile = self.load_projectfile(os.curdir, create=False)
        if not self.lockfile:
            self.lockfile = self.projectfile.model

    @property
    def section_keys(self):
        return set(self.lockfile.keys()) - {"_meta"}

    @property
    def extended_keys(self):
        return list(itertools.product(self.section_keys, ["", "vcs", "editable"]))

    def get(self, k):
        return self.__getitem__(k)

    def __contains__(self, k):
        check_lockfile = k in self.extended_keys or self.lockfile.__contains__(k)
        if check_lockfile:
            return True
        return super().__contains__(k)

    def __setitem__(self, k, v):
        lockfile = self.lockfile
        lockfile.__setitem__(k, v)

    def __getitem__(self, k, *args, **kwargs):
        retval = None
        lockfile = self.lockfile
        try:
            retval = lockfile[k]
        except KeyError:
            if "-" in k:
                section, _, pkg_type = k.rpartition("-")
                vals = getattr(lockfile.get(section, {}), "_data", {})
                if pkg_type == "vcs":
                    retval = {k: v for k, v in vals.items() if is_vcs(v)}
                elif pkg_type == "editable":
                    retval = {k: v for k, v in vals.items() if is_editable(v)}
            if retval is None:
                raise
        else:
            retval = getattr(retval, "_data", retval)
        return retval

    def __getattr__(self, k, *args, **kwargs):
        lockfile = self.lockfile
        try:
            return super().__getattribute__(k)
        except AttributeError:
            retval = getattr(lockfile, k, None)
        if retval is not None:
            return retval
        return super().__getattribute__(k, *args, **kwargs)

    def get_deps(self, dev=False, only=True):
        deps = {}
        if dev:
            deps.update(self.develop._data)
            if only:
                return deps
        deps = merge_items([deps, self.default._data])
        return deps

    @classmethod
    def read_projectfile(cls, path):
        pf = ProjectFile.read(path, lockfiles.Lockfile, invalid_ok=True)
        return pf

    @classmethod
    def lockfile_from_pipfile(cls, pipfile_path):
        from pipenv.utils.pipfile import Pipfile

        # Convert to Path object
        path = Path(pipfile_path)

        if path.is_file():
            # Ensure we have an absolute path
            if not path.is_absolute():
                path = path.resolve()

            # Load the Pipfile from the parent directory
            pipfile = Pipfile.load(path.parent)
            return lockfiles.Lockfile.with_meta_from(pipfile.pipfile)

        raise PipfileNotFound(pipfile_path)

    @classmethod
    def load_projectfile(
        cls, path: Optional[str] = None, create: bool = True, data: Optional[Dict] = None
    ) -> "ProjectFile":
        if not path:
            path = os.curdir
        path = Path(path).absolute()
        project_path = path if path.is_dir() else path.parent
        lockfile_path = path if path.is_file() else project_path / "Pipfile.lock"
        if not project_path.exists():
            raise OSError(f"Project does not exist: {project_path.as_posix()}")
        elif not lockfile_path.exists() and not create:
            raise FileNotFoundError(
                f"Lockfile does not exist: {lockfile_path.as_posix()}"
            )
        projectfile = cls.read_projectfile(lockfile_path.as_posix())
        if not lockfile_path.exists():
            if not data:
                pipfile = project_path.joinpath("Pipfile")
                lf = cls.lockfile_from_pipfile(pipfile)
            else:
                lf = lockfiles.Lockfile(data)
            projectfile.model = lf
        else:
            if data:
                raise ValueError("Cannot pass data when loading existing lockfile")
            with open(lockfile_path.as_posix()) as f:
                projectfile.model = lockfiles.Lockfile.load(f)
        return projectfile

    @classmethod
    def from_data(
        cls, path: Optional[str], data: Optional[Dict], meta_from_project: bool = True
    ) -> "Lockfile":
        if path is None:
            raise MissingParameter("path")
        if data is None:
            raise MissingParameter("data")
        if not isinstance(data, dict):
            raise TypeError("Expecting a dictionary for parameter 'data'")

        # Convert to Path object and resolve to absolute path
        path_obj = Path(path).resolve()

        # Determine project directory
        if path_obj.is_dir():
            project_path = path_obj
        elif not path_obj.is_dir() and path_obj.parent.is_dir():
            project_path = path_obj.parent

        # Create paths for Pipfile and Pipfile.lock
        pipfile_path = project_path / "Pipfile"
        lockfile_path = project_path / "Pipfile.lock"

        if meta_from_project:
            lockfile = cls.lockfile_from_pipfile(pipfile_path)
            lockfile.update(data)
        else:
            lockfile = lockfiles.Lockfile(data)

        projectfile = ProjectFile(
            line_ending=DEFAULT_NEWLINES,
            location=str(
                lockfile_path
            ),  # Convert to string if ProjectFile expects a string
            model=lockfile,
        )

        return cls(
            projectfile=projectfile,
            lockfile=lockfile,
            newlines=projectfile.line_ending,
            path=lockfile_path,  # No need to convert to Path again if already expecting Path
        )

    @classmethod
    def load(cls, path: Optional[str], create: bool = True) -> "Lockfile":
        try:
            projectfile = cls.load_projectfile(path, create=create)
        except JSONDecodeError:
            # Convert to Path object and resolve to absolute path
            path_obj = Path(path).resolve()

            # Determine if the path is a directory or file
            if path_obj.is_dir():
                path_obj = path_obj / "Pipfile.lock"

            # Create backup path
            formatted_path = str(path_obj)
            backup_path = f"{formatted_path}.bak"

            # Show error and create backup
            LockfileCorruptException.show(formatted_path, backup_path=backup_path)
            path_obj.rename(backup_path)

            # Try loading again after backing up corrupted file
            cls.load(formatted_path, create=True)

        # Create Path object from projectfile location
        lockfile_path = Path(projectfile.location)

        # Create instance with required arguments
        creation_args = {
            "projectfile": projectfile,
            "lockfile": projectfile.model,
            "newlines": projectfile.line_ending,
            "path": lockfile_path,
        }
        return cls(**creation_args)

    @classmethod
    def create(cls, path: Optional[str], create: bool = True) -> "Lockfile":
        return cls.load(path, create=create)

    def get_section(self, name: str) -> Optional[Dict]:
        return self.lockfile.get(name)

    @property
    def develop(self) -> Dict:
        return self.lockfile.develop

    @property
    def default(self) -> Dict:
        return self.lockfile.default

    def get_requirements(
        self, dev: bool = True, only: bool = False, categories: Optional[List[str]] = None
    ) -> Iterator[InstallRequirement]:
        from pipenv.utils.requirements import requirement_from_lockfile

        if categories:
            deps = {}
            for category in categories:
                if category == "packages":
                    category = "default"
                elif category == "dev-packages":
                    category = "develop"
                try:
                    category_deps = self[category]
                except KeyError:
                    category_deps = {}
                    self.lockfile[category] = category_deps
                deps = merge_items([deps, category_deps])
        else:
            deps = self.get_deps(dev=dev, only=only)
        for package_name, package_info in deps.items():
            pip_line = requirement_from_lockfile(
                package_name, package_info, include_hashes=False, include_markers=False
            )
            pip_line_specified = requirement_from_lockfile(
                package_name, package_info, include_hashes=True, include_markers=True
            )
            install_req, _ = expansive_install_req_from_line(pip_line)
            yield install_req, pip_line_specified

    def requirements_list(self, category: str) -> List[Dict]:
        if self.lockfile.get(category):
            return [
                {name: entry._data} for name, entry in self.lockfile[category].items()
            ]
        return []

    def write(self) -> None:
        self.projectfile.model = copy.deepcopy(self.lockfile)
        self.projectfile.write()
