import functools
import sys
import os
import tomli
import pkg_resources
import importlib_metadata
from runpy import run_path
from shutil import rmtree, move
from tempfile import mkdtemp
from types import ModuleType
from glob import iglob
from pathlib import Path
from subprocess import check_output, CalledProcessError
from collections import deque
from warnings import warn
from configparser import ConfigParser, SectionProxy
from enum import Enum, auto
from itertools import chain
from typing import (
    Optional,
    Dict,
    Iterable,
    Set,
    Tuple,
    List,
    IO,
    Union,
    Callable,
    Any,
)
from packaging.utils import canonicalize_name
from packaging.requirements import InvalidRequirement, Requirement
from more_itertools import unique_everseen
from ..utilities import lru_cache, run
from ..errors import append_exception_text, get_exception_text

_return_dict_str_str_lru_cache: Callable[
    [], Callable[..., Callable[..., Dict[str, str]]]
] = functools.lru_cache  # type: ignore
_BUILTIN_DISTRIBUTION_NAMES: Tuple[str] = ("distribute",)
# This variable tracks the absolute file paths from which a package has been
# re-installed, in order to avoid performing a reinstall redundantly
_reinstalled_locations: Set[str] = set()


def normalize_name(name: str) -> str:
    """
    Normalize a project/distribution name
    """
    return pkg_resources.safe_name(canonicalize_name(name)).lower()


class ConfigurationFileType(Enum):
    REQUIREMENTS_TXT = auto()
    SETUP_CFG = auto()
    TOX_INI = auto()
    PYPROJECT_TOML = auto()


@lru_cache()
def get_configuration_file_type(path: str) -> ConfigurationFileType:
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    basename: str = os.path.basename(path).lower()
    if basename == "setup.cfg":
        return ConfigurationFileType.SETUP_CFG
    elif basename == "tox.ini":
        return ConfigurationFileType.TOX_INI
    elif basename == "pyproject.toml":
        return ConfigurationFileType.PYPROJECT_TOML
    elif basename.endswith(".txt"):
        return ConfigurationFileType.REQUIREMENTS_TXT
    else:
        raise ValueError(
            f"{path} is not a recognized type of configuration file."
        )


def is_configuration_file(path: str) -> bool:
    try:
        get_configuration_file_type(path)
    except (FileNotFoundError, ValueError):
        return False
    return True


def _get_editable_finder_location(path_name: str) -> str:
    key: str
    value: Any
    init_globals: Dict[str, Any]
    try:
        init_globals = run_path(path_name)
    except Exception:
        return ""
    for key, value in init_globals.items():
        if key.startswith("__editable__"):
            finder: ModuleType = value
            module_name: str
            module_location: str
            for module_name, module_location in getattr(
                finder, "MAPPING", {}
            ).items():
                path: Path = Path(module_location)
                index: int
                for index in range(len(module_name.split("."))):
                    path = path.parent
                while path != path.parent:
                    if (
                        path.joinpath("setup.py").is_file()
                        or path.joinpath("setup.cfg").is_file()
                    ):
                        return str(path)
                    path = path.parent
    return ""


def _iter_path_editable_distribution_locations(
    directory: str,
) -> Iterable[Tuple[str, str]]:
    directory_path: Path = Path(directory)
    file_path: Path
    for file_path in chain(
        directory_path.glob("*.egg-link"),
        directory_path.glob("__editable__.*.pth"),
    ):
        name: str
        if file_path.name.endswith(".egg-link"):
            name = file_path.name[:-9]
        else:
            name = file_path.name[13:-4].partition("-")[0]
        name = normalize_name(name)
        with open(file_path) as file_io:
            location: str = file_io.read().strip().partition("\n")[0]
            if os.path.exists(location):
                yield name, location
            else:
                location = _get_editable_finder_location(str(file_path))
                if location:
                    yield name, location


def _iter_editable_distribution_locations() -> Iterable[Tuple[str, str]]:
    yield from chain(
        *map(_iter_path_editable_distribution_locations, sys.path)
    )


@_return_dict_str_str_lru_cache()
def get_editable_distributions_locations() -> Dict[str, str]:
    """
    Get a mapping of (normalized) editable distribution names to their
    locations.
    """
    return dict(_iter_editable_distribution_locations())


def refresh_working_set() -> None:
    """
    Force a refresh of all distribution information and clear related caches
    """
    get_installed_distributions.cache_clear()
    get_editable_distributions_locations.cache_clear()  # type: ignore
    is_editable.cache_clear()
    is_installed.cache_clear()
    get_requirement_string_distribution_name.cache_clear()
    pkg_resources.working_set.entries = []
    pkg_resources.working_set.__init__()  # type: ignore


def _iter_find_dist_info(directory: Path, project_name: str) -> Iterable[Path]:
    """
    Find all *.dist-info directories for a project in the specified directory
    (there shouldn't be more than one, but pip issues can cause there to
    be on occasions).
    """
    yield from filter(
        Path.is_dir,
        directory.glob(
            f"{pkg_resources.to_filename(project_name)}" "-*.dist-info"
        ),
    )


def _move_dist_info_to_temp_directory(
    directory: Path, project_name: str
) -> Path:
    """
    Move the contents of *.dist-info directories for a project into a temporary
    directory, and return the path to that temp directory.
    """
    temp_directory: Path = Path(mkdtemp())
    dist_info_directory: Path
    for dist_info_directory in _iter_find_dist_info(directory, project_name):
        file_path: Path
        for file_path in dist_info_directory.iterdir():
            move(str(file_path), temp_directory.joinpath(file_path.name))
        rmtree(dist_info_directory)
    return temp_directory


def _merge_directories(
    source_directory: Path, target_directory: Path, overwrite: bool = False
) -> None:
    source_file_path: Path
    target_file_path: Path
    for source_file_path in source_directory.iterdir():
        target_file_path = target_directory.joinpath(source_file_path.name)
        if overwrite or (not target_file_path.exists()):
            move(str(source_file_path), target_file_path)
    rmtree(source_directory)


def refresh_editable_distributions() -> None:
    """
    Update distribution information for editable installs
    """
    name: str
    location: str
    for name, location in get_editable_distributions_locations().items():
        distribution: pkg_resources.Distribution = (
            pkg_resources.get_distribution(name)
        )
        egg_base: Path = Path(getattr(distribution, "egg_info")).parent
        # Find pre-existing dist-info directories, and rename them so that
        # the new dist-info directory doesn't overwrite the old one, then
        # merge the two directories, replacing old files with new files
        # when they exist in both
        temp_directory: Path = _move_dist_info_to_temp_directory(
            egg_base, distribution.project_name
        )
        try:
            if egg_base == location:
                setup_egg_info(location)
            else:
                setup_dist_info(location, egg_base)
        finally:
            _merge_directories(
                temp_directory,
                next(
                    iter(
                        _iter_find_dist_info(
                            egg_base, distribution.project_name
                        )
                    )
                ),
                overwrite=False,
            )
    pkg_resources.working_set.entries = []
    pkg_resources.working_set.__init__()  # type: ignore


@lru_cache()
def get_installed_distributions() -> Dict[str, pkg_resources.Distribution]:
    """
    Return a dictionary of installed distributions.
    """
    refresh_editable_distributions()
    installed: Dict[str, pkg_resources.Distribution] = {}
    for distribution in pkg_resources.working_set:
        installed[normalize_name(distribution.project_name)] = distribution
    return installed


def get_distribution(name: str) -> pkg_resources.Distribution:
    return get_installed_distributions()[normalize_name(name)]


@lru_cache()
def is_installed(distribution_name: str) -> bool:
    return normalize_name(distribution_name) in get_installed_distributions()


def get_requirement_distribution_name(requirement: Requirement) -> str:
    return normalize_name(requirement.name)


@lru_cache()
def get_requirement_string_distribution_name(requirement_string: str) -> str:
    return get_requirement_distribution_name(
        get_requirement(requirement_string)
    )


@lru_cache()
def is_requirement_string(requirement_string: str) -> bool:
    try:
        Requirement(requirement_string)
    except InvalidRequirement:
        return False
    return True


def _iter_file_requirement_strings(path: str) -> Iterable[str]:
    lines: List[str]
    requirement_file_io: IO[str]
    with open(path) as requirement_file_io:
        lines = requirement_file_io.readlines()
    return filter(is_requirement_string, lines)


def _iter_setup_cfg_requirement_strings(path: str) -> Iterable[str]:
    parser: ConfigParser = ConfigParser()
    parser.read(path)
    requirement_strings: Iterable[str] = ()
    if ("options" in parser) and ("install_requires" in parser["options"]):
        requirement_strings = chain(
            requirement_strings,
            filter(
                is_requirement_string,
                parser["options"]["install_requires"].split("\n"),
            ),
        )
    if "options.extras_require" in parser:
        extras_require: SectionProxy = parser["options.extras_require"]
        extra_requirements_string: str
        for extra_requirements_string in extras_require.values():
            requirement_strings = chain(
                requirement_strings,
                filter(
                    is_requirement_string,
                    extra_requirements_string.split("\n"),
                ),
            )
    return unique_everseen(requirement_strings)


def _iter_tox_ini_requirement_strings(path: str) -> Iterable[str]:
    parser: ConfigParser = ConfigParser()
    parser.read(path)

    def get_section_option_requirements(
        section_name: str, option_name: str
    ) -> Iterable[str]:
        if parser.has_option(section_name, option_name):
            return filter(
                is_requirement_string,
                parser.get(section_name, option_name).split("\n"),
            )
        return ()

    def get_section_requirements(section_name: str) -> Iterable[str]:
        requirements: Iterable[str] = get_section_option_requirements(
            section_name, "deps"
        )
        if section_name == "tox":
            requirements = chain(
                requirements,
                get_section_option_requirements(section_name, "requires"),
            )
        return requirements

    return unique_everseen(
        chain(("tox",), *map(get_section_requirements, parser.sections()))
    )


def _iter_pyproject_toml_requirement_strings(path: str) -> Iterable[str]:
    pyproject_io: IO[str]
    with open(path) as pyproject_io:
        pyproject: Dict[str, Any] = tomli.loads(pyproject_io.read())
        if ("build-system" in pyproject) and (
            "requires" in pyproject["build-system"]
        ):
            return pyproject["build-system"]["requires"]
    return ()


def iter_configuration_file_requirement_strings(path: str) -> Iterable[str]:
    """
    Read a configuration file and yield the parsed requirements.
    """
    configuration_file_type: ConfigurationFileType = (
        get_configuration_file_type(path)
    )
    if configuration_file_type == ConfigurationFileType.SETUP_CFG:
        return _iter_setup_cfg_requirement_strings(path)
    elif configuration_file_type == ConfigurationFileType.PYPROJECT_TOML:
        return _iter_pyproject_toml_requirement_strings(path)
    elif configuration_file_type == ConfigurationFileType.TOX_INI:
        return _iter_tox_ini_requirement_strings(path)
    else:
        assert (
            configuration_file_type == ConfigurationFileType.REQUIREMENTS_TXT
        )
        return _iter_file_requirement_strings(path)


@lru_cache()
def is_editable(distribution_project_name: str) -> bool:
    """
    Return `True` if the indicated distribution is an editable installation.
    """
    return bool(
        normalize_name(distribution_project_name)
        in get_editable_distributions_locations()
    )


def _get_setup_cfg_metadata(path: str, key: str) -> str:
    if os.path.basename(path).lower() != "setup.cfg":
        if not os.path.isdir(path):
            path = os.path.dirname(path)
        path = os.path.join(path, "setup.cfg")
    if os.path.isfile(path):
        parser: ConfigParser = ConfigParser()
        parser.read(path)
        if "metadata" in parser:
            return parser.get("metadata", key, fallback="")
        else:
            warn(f"No `metadata` section found in: {path}")
    return ""


def _get_setup_py_metadata(path: str, args: Tuple[str, ...]) -> str:
    """
    Execute a setup.py script with `args` and return the response.

    Parameters:

    - path (str)
    - args ([str])
    """
    value: str = ""
    current_directory: str = os.path.abspath(os.curdir)
    directory: str = path
    try:
        if os.path.basename(path).lower() == "setup.py":
            directory = os.path.dirname(path)
            os.chdir(directory)
        else:
            if not os.path.isdir(path):
                directory = os.path.dirname(path)
            os.chdir(directory)
            path = os.path.join(directory, "setup.py")
        if os.path.isfile(path):
            command: Tuple[str, ...] = (sys.executable, path) + args
            try:
                value = (
                    check_output(
                        command, encoding="utf-8", universal_newlines=True
                    )
                    .strip()
                    .split("\n")[-1]
                )
            except CalledProcessError:
                warn(
                    f"A package name could not be found in {path}, "
                    "attempting to refresh egg info"
                    f"\nError ignored: {get_exception_text()}"
                )
                # re-write egg info and attempt to get the name again
                setup_egg_info(directory)
                try:
                    value = (
                        check_output(
                            command, encoding="utf-8", universal_newlines=True
                        )
                        .strip()
                        .split("\n")[-1]
                    )
                except Exception:
                    warn(
                        f"A package name could not be found in {path}"
                        f"\nError ignored: {get_exception_text()}"
                    )
    finally:
        os.chdir(current_directory)
    return value


def get_setup_distribution_name(path: str) -> str:
    """
    Get a distribution's name from setup.py or setup.cfg
    """
    return normalize_name(
        _get_setup_cfg_metadata(path, "name")
        or _get_setup_py_metadata(path, ("--name",))
    )


def get_setup_distribution_version(path: str) -> str:
    """
    Get a distribution's version from setup.py or setup.cfg
    """
    return _get_setup_cfg_metadata(path, "version") or _get_setup_py_metadata(
        path, ("--version",)
    )


def _setup(arguments: Tuple[str, ...]) -> None:
    try:
        check_output((sys.executable, "setup.py") + arguments)
    except CalledProcessError:
        warn(f"Ignoring error: {get_exception_text()}")


def _setup_location(
    location: Union[str, Path], arguments: Iterable[Tuple[str, ...]]
) -> None:
    if isinstance(location, str):
        location = Path(location)
    # If there is no setup.py file, we can't update egg info
    if not location.joinpath("setup.py").is_file():
        return
    if isinstance(arguments, str):
        arguments = (arguments,)
    current_directory: Path = Path(os.curdir).absolute()
    os.chdir(location)
    try:
        deque(map(_setup, arguments), maxlen=0)
    finally:
        os.chdir(current_directory)


def setup_dist_egg_info(directory: str) -> None:
    """
    Refresh dist-info and egg-info for the editable package installed in
    `directory`
    """
    directory = os.path.abspath(directory)
    if not os.path.isdir(directory):
        directory = os.path.dirname(directory)
    _setup_location(
        directory,
        (
            ("-q", "dist_info"),
            ("-q", "egg_info"),
        ),
    )


def get_editable_distribution_location(name: str) -> str:
    return get_editable_distributions_locations().get(normalize_name(name), "")


def setup_dist_info(directory: str, output_dir: Union[str, Path] = "") -> None:
    """
    Refresh dist-info for the editable package installed in
    `directory`
    """
    directory = os.path.abspath(directory)
    if not os.path.isdir(directory):
        directory = os.path.dirname(directory)
    if isinstance(output_dir, Path):
        output_dir = str(output_dir)
    return _setup_location(
        directory,
        (
            ("-q", "dist_info")
            + (("--output-dir", output_dir) if output_dir else ()),
        ),
    )


def setup_egg_info(directory: Union[str, Path], egg_base: str = "") -> None:
    """
    Refresh egg-info for the editable package installed in
    `directory`
    """
    if isinstance(directory, str):
        directory = Path(directory)
    directory = directory.absolute()
    if not directory.is_dir():
        directory = directory.parent
    # If there is a setup.py, and a *.dist-info directory, but that
    # *.dist-info directory has no RECORD, we need to remove the *.dist-info
    # directory
    if directory.joinpath("setup.py").is_file():
        dist_info: str
        for dist_info in iglob(str(directory.joinpath("*.dist-info"))):
            dist_info_path: Path = Path(dist_info)
            if not dist_info_path.joinpath("RECORD").is_file():
                rmtree(dist_info_path)
    return _setup_location(
        directory,
        (("-q", "egg_info") + (("--egg-base", egg_base) if egg_base else ()),),
    )


def _get_pkg_requirement(
    requirement_string: str,
) -> pkg_resources.Requirement:
    requirement: Union[
        Requirement, pkg_resources.Requirement
    ] = _get_requirement(requirement_string, pkg_resources.Requirement.parse)
    assert isinstance(requirement, pkg_resources.Requirement)
    return requirement


def get_requirement(
    requirement_string: str,
) -> Requirement:
    requirement: Union[
        Requirement, pkg_resources.Requirement
    ] = _get_requirement(requirement_string, Requirement)
    assert isinstance(requirement, Requirement)
    return requirement


def _get_requirement(
    requirement_string: str,
    constructor: Callable[
        [str], Union[Requirement, pkg_resources.Requirement]
    ],
) -> Union[Requirement, pkg_resources.Requirement]:
    try:
        return constructor(requirement_string)
    except (
        InvalidRequirement,
        getattr(
            pkg_resources, "extern"
        ).packaging.requirements.InvalidRequirement,
        getattr(pkg_resources, "RequirementParseError"),
    ):
        # Try to parse the requirement as an installation target location,
        # such as can be used with `pip install`
        location: str = requirement_string
        extras: str = ""
        if "[" in requirement_string and requirement_string.endswith("]"):
            parts: List[str] = requirement_string.split("[")
            location = "[".join(parts[:-1])
            extras = f"[{parts[-1]}"
        location = os.path.abspath(location)
        name: str = get_setup_distribution_name(location)
        assert name, f"No distribution found in {location}"
        return constructor(f"{name}{extras}")


def get_required_distribution_names(
    requirement_string: str,
    exclude: Iterable[str] = (),
    recursive: bool = True,
    echo: bool = False,
) -> Set[str]:
    """
    Return a `set` of all distribution names which are required by the
    distribution specified in `requirement_string`.

    Parameters:

    - requirement_string (str): A distribution name, or a requirement string
      indicating both a distribution name and extras.
    - exclude ([str]): The name of one or more distributions to *exclude*
      from requirements lookup. Please note that excluding a distribution will
      also halt recursive lookup of requirements for that distribution.
    - recursive (bool): If `True` (the default), required distributions will
      be obtained recursively.
    - echo (bool) = False: If `True`, commands and responses executed in
      subprocesses will be printed to `sys.stdout`
    """
    if isinstance(exclude, str):
        exclude = {normalize_name(exclude)}
    else:
        exclude = set(map(normalize_name, exclude))
    return set(
        _iter_requirement_names(
            _get_pkg_requirement(requirement_string),
            exclude=exclude,
            recursive=recursive,
            echo=echo,
        )
    )


def _get_pkg_requirement_name(requirement: pkg_resources.Requirement) -> str:
    return normalize_name(requirement.project_name)


def install_requirement(
    requirement: Union[str, Requirement, pkg_resources.Requirement],
    echo: bool = True,
) -> None:
    """
    Install a requirement

    Parameters:

    - requirement (str)
    - echo (bool) = True: If `True` (default), the `pip install`
      commands will be echoed to `sys.stdout`
    """
    if isinstance(requirement, str):
        requirement = Requirement(requirement)
    return _install_requirement(requirement, echo=echo)


def _install_requirement_string(
    requirement_string: str,
    name: str = "",
    editable: bool = False,
    echo: bool = False,
) -> None:
    uncaught_error: Optional[Exception] = None
    flags: Tuple[str, ...]
    for flags in ((),) + ((("--force-reinstall",),) if editable else ()):
        if editable:
            flags += ("-e",)
        try:
            run(
                (
                    (
                        sys.executable,
                        "-m",
                        "pip",
                        "install",
                        "--no-deps",
                        "--no-compile",
                        "--no-build-isolation",
                    )
                    + flags
                    + (requirement_string,)
                ),
                echo=echo,
            )
            uncaught_error = None
            break
        except CalledProcessError as error:
            if (uncaught_error is None) or (not flags):
                uncaught_error = error
    if uncaught_error is not None:
        append_exception_text(
            uncaught_error,
            (
                f"\nCould not install {name}"
                if name == requirement_string
                else (f"\nCould not install {name} from {requirement_string}")
            )
            if name
            else (f"\nCould not install {requirement_string}"),
        )
        raise uncaught_error


def _install_requirement(
    requirement: Union[Requirement, pkg_resources.Requirement],
    echo: bool = True,
) -> None:
    requirement_string: str = str(requirement)
    # Get the distribution name
    name: str = normalize_name(
        requirement.name
        if isinstance(requirement, Requirement)
        else requirement.project_name
    )
    distribution: Optional[pkg_resources.Distribution] = None
    editable_location: str = ""
    try:
        distribution = get_distribution(name)
        editable_location = get_editable_distribution_location(
            distribution.project_name
        )
    except KeyError:
        pass
    # If the requirement is installed and editable, re-install from
    # the editable location
    if distribution and editable_location:
        # Assemble a requirement specifier for the editable install
        requirement_string = editable_location
        if requirement.extras:
            requirement_string = (
                f"{requirement_string}[{','.join(requirement.extras)}]"
            )
    _install_requirement_string(
        requirement_string=requirement_string,
        name=name,
        editable=bool(editable_location),
        echo=echo,
    )
    # Refresh the metadata
    refresh_working_set()


def _get_pkg_requirement_distribution(
    requirement: pkg_resources.Requirement,
    name: str,
    reinstall: bool = True,
    echo: bool = False,
) -> Optional[pkg_resources.Distribution]:
    if name in _BUILTIN_DISTRIBUTION_NAMES:
        return None
    try:
        return get_installed_distributions()[name]
    except KeyError:
        if not reinstall:
            raise
        if echo:
            warn(
                f'The required distribution "{name}" was not installed, '
                "attempting to install it now..."
            )
        # Attempt to install the requirement...
        install_requirement(requirement, echo=echo)
        return _get_pkg_requirement_distribution(
            requirement, name, reinstall=False, echo=echo
        )


def _iter_requirement_names(
    requirement: pkg_resources.Requirement,
    exclude: Set[str],
    recursive: bool = True,
    echo: bool = False,
) -> Iterable[str]:
    name: str = normalize_name(requirement.project_name)
    extras: Set[str] = set(map(normalize_name, requirement.extras))
    if name in exclude:
        return ()
    # Ensure we don't follow the same requirement again, causing cyclic
    # recursion
    exclude.add(name)
    distribution: Optional[
        pkg_resources.Distribution
    ] = _get_pkg_requirement_distribution(requirement, name, echo=echo)
    if distribution is None:
        return ()
    requirements: List[pkg_resources.Requirement] = distribution.requires(
        extras=tuple(sorted(extras))
    )
    lateral_exclude: Set[str] = set()

    def iter_requirement_names_(
        requirement_: pkg_resources.Requirement,
    ) -> Iterable[str]:
        return _iter_requirement_names(
            requirement_,
            exclude=(
                exclude
                | (lateral_exclude - {_get_pkg_requirement_name(requirement_)})
            ),
            recursive=recursive,
            echo=echo,
        )

    def not_excluded(name: str) -> bool:
        if name not in exclude:
            # Add this to the exclusions
            lateral_exclude.add(name)
            return True
        return False

    if recursive:
        requirement_names = chain(
            filter(not_excluded, map(_get_pkg_requirement_name, requirements)),
            *map(iter_requirement_names_, requirements),
        )
    return requirement_names


def _iter_requirement_strings_required_distribution_names(
    requirement_strings: Iterable[str],
    echo: bool = False,
) -> Iterable[str]:
    visited_requirement_strings: Set[str] = set()
    if isinstance(requirement_strings, str):
        requirement_strings = (requirement_strings,)

    def get_required_distribution_names_(requirement_string: str) -> Set[str]:
        if requirement_string not in visited_requirement_strings:
            try:
                name: str = get_requirement_string_distribution_name(
                    requirement_string
                )
                visited_requirement_strings.add(requirement_string)
                return get_required_distribution_names(
                    requirement_string, echo=echo
                ) | {name}
            except KeyError:
                pass
        return set()

    return unique_everseen(
        chain(*map(get_required_distribution_names_, requirement_strings)),
    )


def get_requirements_required_distribution_names(
    requirements: Iterable[str] = (),
    echo: bool = False,
) -> Set[str]:
    """
    Get the distributions required by one or more specified distributions or
    configuration files.

    Parameters:

    - requirements ([str]): One or more requirement specifiers (for example:
      "requirement-name[extra-a,extra-b]" or ".[extra-a, extra-b]) and/or paths
      to a setup.cfg, pyproject.toml, tox.ini or requirements.txt file
    """
    # Separate requirement strings from requirement files
    if isinstance(requirements, str):
        requirements = {requirements}
    else:
        requirements = set(requirements)
    requirement_files: Set[str] = set(
        filter(is_configuration_file, requirements)
    )
    requirement_strings: Set[str] = requirements - requirement_files
    name: str
    return set(
        sorted(
            _iter_requirement_strings_required_distribution_names(
                unique_everseen(
                    chain(
                        requirement_strings,
                        *map(
                            iter_configuration_file_requirement_strings,
                            requirement_files,
                        ),
                    )
                ),
                echo=echo,
            ),
            key=lambda name: name.lower(),
        )
    )


def iter_distribution_location_file_paths(location: str) -> Iterable[str]:
    location = os.path.abspath(location)
    name: str = get_setup_distribution_name(location)
    setup_egg_info(location)
    metadata_path: str = os.path.join(
        location, f"{pkg_resources.to_filename(name)}.egg-info"
    )
    distribution: importlib_metadata.Distribution = (
        importlib_metadata.Distribution.at(metadata_path)
    )
    if not distribution.files:
        raise RuntimeError(f"No metadata found at {metadata_path}")
    return map(os.path.abspath, distribution.files)
