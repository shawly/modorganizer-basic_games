from __future__ import annotations

import configparser
import fnmatch
from enum import Enum
from pathlib import Path
from typing import List, Set, Tuple, Union

from PyQt6.QtCore import QDateTime, QDir, qInfo, qWarning
from PyQt6.QtWidgets import QMessageBox

import mobase

from ..basic_features import BasicLocalSavegames, BasicModDataChecker, GlobPatterns
from ..basic_features.basic_save_game_info import BasicGameSaveGame, BasicGameSaveGameInfo
from ..basic_game import BasicGame
from .witcher3 import settings_merge as merge_mod


def _qlog(msg: str, warn: bool = False) -> None:
    """Logs to MO2; PyQt's qInfo/qWarning encode str as ASCII, so the message is passed as UTF-8 bytes."""
    (qWarning if warn else qInfo)(f"[Witcher 3] {msg}".encode("utf-8"))

DX11_EXE_REL: str = "bin/x64/witcher3.exe"
DX12_EXE_REL: str = "bin/x64_dx12/witcher3.exe"

DEFAULT_MODS_FOLDER: str = "Mods"
DEFAULT_DLC_FOLDER: str = "DLC"
FRAGMENT_DIR_NAME: str = merge_mod.FRAGMENT_DIR_NAME
PLUGIN_NAME: str = "Witcher 3 Support Plugin"

DELETE_PATTERNS: List[str] = [
    "*.gif", "*.jpg", "*.jpeg", "*.md", "*.png", "*.txt",
    "*.pdf", "*.html", "*.url", "*.doc", "*.docx", "*.rtf", "*.log", "*.bak",
]


class Witcher3Edition(Enum):
    """Game edition, derived from which executables exist."""

    NEXT_GEN = "Next-Gen"
    CLASSIC = "Classic"
    REMASTERED = "Remastered"
    UNKNOWN = "Unknown"


def _plugin_setting(organizer: mobase.IOrganizer | None, key: str) -> mobase.MoVariant:
    """Reads a setting of this plugin; returns None if unavailable."""
    if organizer is None:
        return None
    try:
        return organizer.pluginSetting(PLUGIN_NAME, key)
    except Exception:
        return None


def _is_merge_enabled(organizer: mobase.IOrganizer | None) -> bool:
    """Whether the experimental settings merge is enabled."""
    return bool(_plugin_setting(organizer, "experimental_merge_settings"))


def _detect_game_edition(game_dir: QDir) -> Witcher3Edition:
    """Detects the edition: both executables = Next-Gen, DX11 only = Classic, DX12 only = Remastered."""
    try:
        if game_dir.path() in ("", ".") or not game_dir.exists():
            return Witcher3Edition.UNKNOWN
        has_dx11 = game_dir.exists(DX11_EXE_REL)
        has_dx12 = game_dir.exists(DX12_EXE_REL)

        if has_dx11 and has_dx12:
            return Witcher3Edition.NEXT_GEN
        if has_dx11:
            return Witcher3Edition.CLASSIC
        if has_dx12:
            return Witcher3Edition.REMASTERED
        return Witcher3Edition.UNKNOWN
    except Exception as e:
        _qlog(f"Failed to detect game edition: {e}", True)
        return Witcher3Edition.UNKNOWN


def _safe_remove_entry(target: mobase.IFileTree, entry: mobase.FileTreeEntry, context_label: str = "file") -> bool:
    """Removes an entry from the tree (detach first, remove() as fallback); returns success."""
    try:
        if not entry.detach():
            target.remove(entry)
        return True
    except Exception:
        try:
            target.remove(entry)
            return True
        except Exception as e:
            _qlog(f"Failed to remove {context_label} '{entry.name()}': {e}", True)
            return False


def _is_mod_or_dlc_dir(entry: mobase.FileTreeEntry) -> bool:
    """Whether entry is an individual mod/DLC folder (e.g. 'modFoo', 'dlcBar'), not the 'Mods'/'DLC' container."""
    if not entry.isDir():
        return False
    nl = entry.name().strip().casefold()
    return (nl.startswith("mod") and nl != "mods") or (nl.startswith("dlc") and nl != "dlc")


def _is_container_dir(entry: mobase.FileTreeEntry) -> bool:
    """Whether entry is a known top-level folder (Mods, DLC, bin, W3_SettingsFragments)."""
    if not entry.isDir():
        return False
    return entry.name().strip().casefold() in ("mods", "dlc", "bin", FRAGMENT_DIR_NAME.casefold())


def _is_wrapper_dir(entry: mobase.FileTreeEntry) -> bool:
    """Whether entry is any other folder, i.e. a potential archive wrapper folder."""
    return entry.isDir() and not _is_container_dir(entry) and not _is_mod_or_dlc_dir(entry)


def _is_fragment(entry: mobase.FileTreeEntry) -> bool:
    """Whether entry is a *user.settings / *input.settings fragment file."""
    return not entry.isDir() and merge_mod is not None and merge_mod.fragment_kind(entry.name()) is not None


def _is_junk(name: str) -> bool:
    """Whether the filename matches DELETE_PATTERNS (readmes, images, ...)."""
    name_lower = name.lower()
    return any(fnmatch.fnmatch(name_lower, p.lower()) for p in DELETE_PATTERNS)


def _get_tree_obj(tree: mobase.IFileTree | mobase.FileTreeEntry) -> mobase.IFileTree | None:
    """Returns the IFileTree behind a tree or directory entry, or None."""
    if isinstance(tree, mobase.IFileTree):
        return tree
    for attr in ("astree", "asTree"):
        if hasattr(tree, attr):
            try:
                res = getattr(tree, attr)()
                if res is not None:
                    return res
            except Exception as e:
                _qlog(f"Failed to resolve directory tree via {attr}(): {e}", True)
    return None


def _children(tree: mobase.IFileTree | mobase.FileTreeEntry) -> list:
    """Child entries of a tree or directory entry; [] if not a directory."""
    target = _get_tree_obj(tree)
    if target is None or not hasattr(target, "__iter__"):
        return []
    try:
        return list(target)
    except Exception as e:
        _qlog(f"Failed to list tree children: {e}", True)
        return []


def _origin_name(entry: mobase.FileTreeEntry) -> str:
    """Name of the nearest mod/DLC ancestor folder, or "" if none."""
    p = entry.parent()
    while p is not None:
        if _is_mod_or_dlc_dir(p):
            return p.name()
        p = p.parent()
    return ""


def _iter_files(tree: mobase.IFileTree | mobase.FileTreeEntry):
    """Recursively yields (file entry, parent tree), skipping W3_SettingsFragments."""
    target = _get_tree_obj(tree)
    for entry in _children(tree):
        if entry.isDir():
            if entry.name().strip().casefold() == FRAGMENT_DIR_NAME.casefold():
                continue
            yield from _iter_files(entry)
        else:
            yield entry, target


def _warn_bundled_settings(found_files: List[str]) -> None:
    """Logs and shows a dialog explaining that the fragments must be merged manually."""
    if not found_files:
        return
    file_list = "\n".join(f"• {f}" for f in found_files)
    _qlog(f"Settings fragments stored in {FRAGMENT_DIR_NAME}: {', '.join(found_files)}", True)
    try:
        msg = QMessageBox()
        msg.setIcon(QMessageBox.Icon.Information)
        msg.setWindowTitle("Witcher 3 Mod - Settings Fragments Preserved")
        msg.setText("<b>Settings Fragments Preserved in Mod Folder</b>")
        msg.setInformativeText(
            f"This mod contains settings fragment(s):\n\n{file_list}\n\n"
            f"The files were NOT discarded. They are stored in the mod's '{FRAGMENT_DIR_NAME}' folder "
            "(in MO2: right-click the mod > 'Open in Explorer').\n"
            "You must manually merge these settings into your game files using the MO2 INI Editor, "
            "or enable automatic settings merging in plugin settings."
        )
        msg.setStandardButtons(QMessageBox.StandardButton.Ok)
        msg.exec()
    except Exception as e:
        _qlog(f"Failed to display settings warning popup: {e}", True)


def _report_fragments(moved: List[str], organizer: mobase.IOrganizer | None) -> None:
    """Reports preserved fragments: log only if the merge is enabled, otherwise also a dialog."""
    if not moved:
        return
    if _is_merge_enabled(organizer):
        _qlog(f"Preserved {len(moved)} settings fragment(s) in {FRAGMENT_DIR_NAME}: {', '.join(moved)}")
    else:
        _warn_bundled_settings(moved)


def _has_fomod(tree: mobase.IFileTree | mobase.FileTreeEntry, depth: int = 0, max_depth: int = 4) -> bool:
    """Whether the archive contains a FOMOD installer (left to the FOMOD installer)."""
    if depth > max_depth:
        return False
    for entry in _children(tree):
        nl = entry.name().lower()
        if nl in ("fomod", "moduleconfig.xml"):
            return True
        if _is_wrapper_dir(entry):
            if _has_fomod(entry, depth + 1, max_depth):
                return True
    return False


def _has_witcher_content(tree: mobase.IFileTree | mobase.FileTreeEntry, depth: int = 0, max_depth: int = 3) -> bool:
    """Whether the tree contains mod/DLC/container folders or fragments within max_depth levels."""
    for entry in _children(tree):
        if _is_mod_or_dlc_dir(entry) or _is_container_dir(entry):
            return True
        if _is_fragment(entry):
            return True
        if entry.isDir() and depth < max_depth:
            if _has_witcher_content(entry, depth + 1, max_depth):
                return True
    return False


def _find_ambiguous_wrappers(
    tree: mobase.IFileTree | mobase.FileTreeEntry, depth: int = 0, max_depth: int = 3
) -> Tuple[bool, List[str]]:
    """Detects several wrapper folders with content (e.g. variants); returns (ambiguous, folder names)."""
    if depth >= max_depth:
        return False, []

    candidates = [
        e for e in _children(tree)
        if _is_wrapper_dir(e) and _has_witcher_content(e, depth + 1, max_depth)
    ]

    if len(candidates) > 1:
        return True, [e.name() for e in candidates]
    if len(candidates) == 1:
        return _find_ambiguous_wrappers(candidates[0], depth + 1, max_depth)
    return False, []


def _unwrap_tree(tree: mobase.IFileTree) -> None:
    """Moves the content of a single wrapper folder up to the root, repeated up to 3 levels."""
    for _ in range(3):
        if any(_is_mod_or_dlc_dir(e) or _is_container_dir(e) for e in _children(tree)):
            break
        wrappers = [
            e
            for e in _children(tree)
            if _is_wrapper_dir(e) and _has_witcher_content(e)
        ]
        if len(wrappers) != 1:
            break
        w_entry = wrappers[0]
        w_children = _children(w_entry)
        if not w_children:
            break

        all_moved = True
        for child in w_children:
            try:
                if not tree.insert(child, mobase.IFileTree.InsertPolicy.MERGE):
                    all_moved = False
            except Exception as e:
                all_moved = False
                _qlog(f"Failed to unwrap child '{child.name()}' from wrapper '{w_entry.name()}': {e}", True)

        if all_moved and not _children(w_entry):
            _safe_remove_entry(tree, w_entry, "empty wrapper")
        else:
            break


def _normalize_witcher_tree(
    tree: mobase.IFileTree,
    mods_folder: str = DEFAULT_MODS_FOLDER,
    dlc_folder: str = DEFAULT_DLC_FOLDER,
) -> None:
    """Unwraps wrappers, collects fragments in W3_SettingsFragments, deletes junk and moves mod*/dlc* folders into Mods/DLC."""
    _unwrap_tree(tree)

    if merge_mod is not None:
        frag_entries = [
            (e, _origin_name(e))
            for e, _ in _iter_files(tree)
            if _is_fragment(e)
        ]
        if frag_entries:
            frag_dir_entry = tree.find(FRAGMENT_DIR_NAME) or tree.addDirectory(FRAGMENT_DIR_NAME)
            frag_tree = _get_tree_obj(frag_dir_entry)
            existing_names = [e.name() for e in _children(frag_tree)] if frag_tree is not None else []

            for entry, origin in frag_entries:
                new_name = merge_mod.unique_fragment_name(existing_names, entry.name(), origin)
                existing_names.append(new_name)
                target_path = f"{FRAGMENT_DIR_NAME}/{new_name}"
                try:
                    moved = tree.move(entry, target_path)
                except Exception as e:
                    moved = False
                    _qlog(f"Failed to move fragment '{entry.name()}': {e}", True)
                if not moved:
                    _qlog(f"Fragment '{entry.name()}' could not be moved; kept in place", True)

    for entry, parent_t in list(_iter_files(tree)):
        # Files under bin/ are game config (e.g. dx12filelist.txt), never readmes
        if entry.path("/").casefold().startswith("bin/"):
            continue
        if _is_junk(entry.name()) and not _is_fragment(entry):
            _safe_remove_entry(parent_t, entry, "junk file")

    for entry in list(tree):
        if _is_mod_or_dlc_dir(entry):
            nl = entry.name().lower()
            container_name = mods_folder if nl.startswith("mod") else dlc_folder if nl.startswith("dlc") else None
            if container_name:
                container_entry = tree.find(container_name) or tree.addDirectory(container_name)
                container_tree = _get_tree_obj(container_entry)
                if container_tree is not None:
                    try:
                        container_tree.insert(entry, mobase.IFileTree.InsertPolicy.MERGE)
                    except Exception as e:
                        _qlog(f"Failed to insert '{entry.name()}' into '{container_name}': {e}", True)


def _active_mod_bases(organizer: mobase.IOrganizer, reverse: bool = False) -> List[Path]:
    """Paths of active mods in ascending MO2 priority with Overwrite last; reverse=True flips the order."""
    mods_path = Path(organizer.modsPath())
    overwrite_path = Path(organizer.overwritePath())
    modlist = organizer.modList()

    ordered_mods = modlist.allModsByProfilePriority()
    candidate_paths: List[Path] = []

    if reverse:
        candidate_paths.append(overwrite_path)
        ordered_mods = list(reversed(ordered_mods))

    for mod_name in ordered_mods:
        if modlist.state(mod_name) & mobase.ModState.ACTIVE:
            candidate_paths.append(mods_path / mod_name)

    if not reverse:
        candidate_paths.append(overwrite_path)

    return candidate_paths


class Witcher3ModDataChecker(BasicModDataChecker):
    """Checks and fixes the layout of Witcher 3 mods in MO2."""

    def __init__(
        self,
        mods_folder: str = DEFAULT_MODS_FOLDER,
        dlc_folder: str = DEFAULT_DLC_FOLDER,
    ):
        self._mods_folder = mods_folder
        self._dlc_folder = dlc_folder
        super().__init__(GlobPatterns())

    def dataLooksValid(self, tree: mobase.IFileTree, _depth: int = 0) -> mobase.ModDataChecker.CheckReturn:
        """Checks the layout; parents are checked too, so a fixable parent makes the subtree fixable."""
        if _depth < 9:
            parent = tree.parent()
            if parent is not None and parent != tree:
                if self.dataLooksValid(parent, _depth + 1) == mobase.ModDataChecker.FIXABLE:
                    return mobase.ModDataChecker.FIXABLE

        is_ambiguous, _ = _find_ambiguous_wrappers(tree)
        if is_ambiguous:
            return mobase.ModDataChecker.INVALID

        if merge_mod is not None and any(_is_fragment(e) for e, _ in _iter_files(tree)):
            return mobase.ModDataChecker.FIXABLE

        if not _has_witcher_content(tree):
            return mobase.ModDataChecker.INVALID

        has_fixable_items = False

        for entry in tree:
            name_lower = entry.name().lower()
            if entry.isDir() and name_lower in (self._mods_folder.lower(), self._dlc_folder.lower(), "bin", FRAGMENT_DIR_NAME.casefold()):
                continue

            if _is_fragment(entry):
                has_fixable_items = True
                continue

            if not entry.isDir() and _is_junk(entry.name()):
                has_fixable_items = True
                continue

            if entry.isDir() and _is_mod_or_dlc_dir(entry):
                has_fixable_items = True
                continue

            if entry.isDir() and _has_witcher_content(entry):
                has_fixable_items = True
                continue

            return mobase.ModDataChecker.INVALID

        return mobase.ModDataChecker.FIXABLE if has_fixable_items else mobase.ModDataChecker.VALID

    def fix(self, tree: mobase.IFileTree) -> mobase.IFileTree:
        """Fixes the layout; raises on ambiguous archives with several variants."""
        is_ambiguous, candidate_names = _find_ambiguous_wrappers(tree)
        if is_ambiguous:
            names = ", ".join(f"'{n}'" for n in candidate_names)
            msg = f"Detected multiple mod variants ({names}) in archive."
            _qlog(msg, True)
            raise RuntimeError(msg)

        _normalize_witcher_tree(tree, self._mods_folder, self._dlc_folder)
        return tree


class Witcher3NestedInstaller(mobase.IPluginInstallerSimple):
    """Simple installer for archives with loose or nested mod/DLC folders (not FOMOD)."""

    def __init__(self):
        super().__init__()
        self._organizer: mobase.IOrganizer | None = None

    def init(self, organizer: mobase.IOrganizer) -> bool:
        self._organizer = organizer
        return True

    def name(self) -> str:
        return "Witcher 3 Loose & Nested Archive Installer"

    def localizedName(self) -> str:
        return "Witcher 3 Installer for Loose and Nested Archives"

    def author(self) -> str:
        return "ShiftingPixel"

    def description(self) -> str:
        return "Reorganizes Witcher 3 mod archives containing loose/nested mod and dlc folders into Mods/ and DLC/."

    def version(self) -> mobase.VersionInfo:
        return mobase.VersionInfo(2, 2, 0)

    def priority(self) -> int:
        return 150

    def isManualInstaller(self) -> bool:
        return False

    def isActive(self) -> bool:
        if self._organizer is None:
            return False
        game = self._organizer.managedGame()
        return game is not None and game.gameShortName() in Witcher3Game.SUPPORTED_GAME_SHORT_NAMES

    def isArchiveSupported(self, tree: mobase.IFileTree) -> bool:
        """Supported if Witcher 3 is managed and the archive has content, no FOMOD and no variants."""
        if not self.isActive() or _has_fomod(tree):
            return False
        is_ambiguous, _ = _find_ambiguous_wrappers(tree)
        if is_ambiguous:
            return False
        return _has_witcher_content(tree)

    def install(
        self, name: mobase.GuessedString, tree: mobase.IFileTree, version: str, nexus_id: int
    ) -> Union[mobase.InstallResult, mobase.IFileTree, Tuple[mobase.InstallResult, mobase.IFileTree]]:
        """Installs the archive with a normalized layout."""
        if not self.isArchiveSupported(tree):
            return mobase.InstallResult.NOT_ATTEMPTED

        _normalize_witcher_tree(tree, DEFAULT_MODS_FOLDER, DEFAULT_DLC_FOLDER)
        return (mobase.InstallResult.SUCCESS, tree)


class Witcher3SaveGame(BasicGameSaveGame):
    """Witcher 3 save including its screenshot sidecar file."""

    def allFiles(self) -> List[str]:
        files = [self._filepath.name]
        for ext in (".png", ".bmp"):
            sidecar = self._filepath.with_suffix(ext)
            if sidecar.exists():
                files.append(sidecar.name)
        return files

    def getCreationTime(self) -> QDateTime:
        return QDateTime.fromSecsSinceEpoch(int(self._filepath.stat().st_mtime))


class Witcher3Game(BasicGame):
    """MO2 game plugin for The Witcher 3: Wild Hunt."""

    Name = PLUGIN_NAME
    Author = "ShiftingPixel"
    Version = "2.2.0"

    GameName = "The Witcher 3: Wild Hunt"
    GameShortName = "witcher3"
    SUPPORTED_GAME_SHORT_NAMES: Set[str] = {"witcher3"}

    GameNexusName = "witcher3"
    GameNexusId = 952
    GameSteamId = [499450, 292030]
    GameGogId = [1640424747, 1495134320, 1207664663, 1207664643]
    GameBinary = DX11_EXE_REL
    GameDataPath = ""
    GameSaveExtension = "sav"
    GameDocumentsDirectory = "%DOCUMENTS%/The Witcher 3"
    GameSavesDirectory = "%GAME_DOCUMENTS%/gamesaves"

    _mods_folder_name: str = DEFAULT_MODS_FOLDER
    _dlc_folder_name: str = DEFAULT_DLC_FOLDER
    _mods_settings_filename: str = "mods.settings"
    GameSupportURL = "https://github.com/ModOrganizer2/modorganizer-basic_games/wiki/Game:-The-Witcher-3"

    _last_logged_edition: Tuple[str, Witcher3Edition] | None = None

    def init(self, organizer: mobase.IOrganizer) -> bool:
        """Registers game features and the pre-launch and mod-install hooks."""
        super().init(organizer)
        self._register_feature(BasicLocalSavegames(self))
        self._register_feature(BasicGameSaveGameInfo(lambda p: Path(p).with_suffix(".png")))
        self._register_feature(Witcher3ModDataChecker(self._mods_folder_name, self._dlc_folder_name))
        organizer.onAboutToRun(self._onAboutToRun)
        try:
            organizer.modList().onModInstalled(self._onModInstalled)
        except Exception as e:
            _qlog(f"Failed to register onModInstalled hook: {e}", True)
        return True

    def _onModInstalled(self, mod: mobase.IModInterface) -> None:
        """Reports preserved settings fragments after a mod is installed."""
        if not self.isActive():
            return
        try:
            frag_dir = Path(mod.absolutePath()) / FRAGMENT_DIR_NAME
            if frag_dir.is_dir():
                fragments = sorted(
                    p.name for p in frag_dir.iterdir()
                    if p.is_file() and (merge_mod is None or merge_mod.fragment_kind(p.name) is not None)
                )
                _report_fragments(fragments, self._organizer)
        except Exception as e:
            _qlog(f"Failed to inspect settings fragments after mod install: {e}", True)

    def setGamePath(self, path: QDir | str | Path) -> None:
        """Sets the game directory and logs the detected edition once per path."""
        try:
            super().setGamePath(path)
        except Exception as e:
            _qlog(f"Failed to set game path: {e}", True)

        try:
            game_dir_str = self.gameDirectory().absolutePath()
            edition = self.gameEdition()
            key = (game_dir_str, edition)
            if self._last_logged_edition != key:
                self._last_logged_edition = key
                _qlog(f"Detected game edition: {edition.value}")
        except Exception as e:
            _qlog(f"Failed to log game edition: {e}", True)

    def gameEdition(self) -> Witcher3Edition:
        """Edition of the installed game."""
        return _detect_game_edition(self.gameDirectory())

    def binaryName(self) -> str:
        """DX12 executable for Remastered (DX12 only), DX11 otherwise."""
        return DX12_EXE_REL if self.gameEdition() == Witcher3Edition.REMASTERED else DX11_EXE_REL

    def looksValid(self, directory: QDir | str | Path) -> bool:
        """Valid if the DX11 or DX12 executable exists."""
        try:
            if isinstance(directory, QDir):
                return directory.exists(DX11_EXE_REL) or directory.exists(DX12_EXE_REL)
            p = Path(directory)
            return (p / DX11_EXE_REL).exists() or (p / DX12_EXE_REL).exists()
        except Exception as e:
            _qlog(f"Failed to validate game path: {e}", True)
            return False

    def isGamePathValid(self, folder: QDir) -> bool:
        return self.looksValid(folder)

    def executables(self) -> List[mobase.ExecutableInfo]:
        """One entry per existing executable, labeled (DX11)/(DX12) if both exist."""
        game_dir = self.gameDirectory()
        candidates = [(DX12_EXE_REL, " (DX12)"), (DX11_EXE_REL, " (DX11)")]
        present = [(rel, label) for rel, label in candidates if game_dir.exists(rel)]
        both = len(present) == 2
        return [
            mobase.ExecutableInfo(f"{self.gameName()}{label if both else ''}", game_dir.absoluteFilePath(rel))
            for rel, label in present
        ]

    def iniFiles(self) -> List[str]:
        return ["user.settings", "dx12user.settings", "input.settings", self._mods_settings_filename]

    def listSaves(self, folder: QDir) -> List[mobase.ISaveGame]:
        if not folder.exists():
            return []
        ext = self.GameSaveExtension
        if hasattr(self, "_mappings") and hasattr(self._mappings, "savegameExtension"):
            try:
                ext = self._mappings.savegameExtension.get()
            except Exception as e:
                _qlog(f"Failed to query savegameExtension mapping: {e}", True)
        return [Witcher3SaveGame(p) for p in Path(folder.absolutePath()).glob(f"*.{ext}")]

    def settings(self) -> List[mobase.PluginSetting]:
        return [
            mobase.PluginSetting("enforce_mods_settings", "Automatically generate and enforce mods.settings before launch", True),
            mobase.PluginSetting(
                "reverse_mods_settings_load_order",
                "Reverse MO2 priority when writing mods.settings "
                "(Enabled: Last mod in MO2 gets Witcher 3 Priority 0. Disabled: First mod in MO2 gets Witcher 3 Priority 0)",
                True,
            ),
            mobase.PluginSetting(
                "experimental_merge_settings",
                "[EXPERIMENTAL] Automatically merge .settings fragments shipped by mods into the game .settings files at launch",
                False,
            ),
        ]

    def _onAboutToRun(self, app_path_str: str, wd: QDir, args: str) -> bool:
        """Before launch: writes mods.settings and runs (or rolls back) the settings merge; never blocks the launch."""
        if not self.isActive():
            return True
        try:
            if _plugin_setting(self._organizer, "enforce_mods_settings"):
                self._update_mods_settings()
        except Exception as e:
            _qlog(f"Error updating mods.settings before launch: {e}", True)

        try:
            self._run_experimental_settings_merge(_is_merge_enabled(self._organizer))
        except Exception as e:
            _qlog(f"Error during experimental settings merge before launch: {e}", True)

        return True

    def _get_mods_settings_path(self) -> Path:
        """mods.settings path: profile folder with profile-specific INIs, otherwise Documents/The Witcher 3."""
        profile = self._organizer.profile()
        if profile and profile.localSettingsEnabled():
            return Path(profile.absolutePath()) / self._mods_settings_filename
        return Path(self.documentsDirectory().absolutePath()) / self._mods_settings_filename

    def _get_active_mod_folders_for_merge(self) -> List[Path]:
        """W3_SettingsFragments folders of active mods in ascending MO2 priority (Overwrite last)."""
        frag_dirs = (base / FRAGMENT_DIR_NAME for base in _active_mod_bases(self._organizer, reverse=False))
        return [d for d in frag_dirs if d.is_dir()]

    @staticmethod
    def _log_merge(msg: str, warn: bool = False) -> None:
        """Forwards settings merge log messages to the MO2 log."""
        _qlog(msg, warn)

    def _run_experimental_settings_merge(self, enabled: bool = True) -> None:
        """Runs the settings merge; when disabled, rolls back all previously merged entries."""
        if merge_mod is None:
            return
        target_dir = self._get_mods_settings_path().parent
        fragment_dirs = self._get_active_mod_folders_for_merge() if enabled else []
        merge_mod.merge_settings(fragment_dirs, target_dir, self._log_merge)

    @staticmethod
    def _find_child_dir_case_insensitive(parent: Path, target_name: str) -> Path | None:
        """Child directory matching target_name case-insensitively, or None."""
        if not parent.exists() or not parent.is_dir():
            return None
        target_casefold = target_name.casefold()
        try:
            for child in parent.iterdir():
                if child.is_dir() and child.name.casefold() == target_casefold:
                    return child
        except OSError:
            pass
        return None

    def _get_active_mod_folders(self) -> List[str]:
        """Unique mod* folder names inside Mods/ of all active mods, in the order written to mods.settings."""
        reverse = bool(_plugin_setting(self._organizer, "reverse_mods_settings_load_order"))
        discovered: List[str] = []
        seen: Set[str] = set()
        for base_path in _active_mod_bases(self._organizer, reverse=reverse):
            mods_dir = self._find_child_dir_case_insensitive(base_path, self._mods_folder_name)
            if mods_dir is None:
                continue
            for child in sorted(mods_dir.iterdir(), key=lambda p: p.name.casefold()):
                key = child.name.casefold()
                # First occurrence wins, so the mod listed first keeps its position
                if child.is_dir() and key.strip().startswith("mod") and key not in seen:
                    seen.add(key)
                    discovered.append(child.name)
        return discovered

    def _update_mods_settings(self) -> Path:
        """Writes mods.settings (UTF-8, no BOM, Priority 0 = highest); deletes it if no mods are active."""
        mods_settings_path = self._get_mods_settings_path()
        mod_folders = self._get_active_mod_folders()

        if not mod_folders:
            if mods_settings_path.exists():
                try:
                    mods_settings_path.unlink()
                except OSError as e:
                    _qlog(f"Failed to remove unused mods.settings: {e}", True)
            return mods_settings_path

        mods_settings_path.parent.mkdir(parents=True, exist_ok=True)
        config = configparser.ConfigParser()
        config.optionxform = str  # type: ignore

        for priority, folder_name in enumerate(mod_folders):
            config.add_section(folder_name)
            config.set(folder_name, "Enabled", "1")
            config.set(folder_name, "Priority", str(priority))

        with open(mods_settings_path, "w", encoding="utf-8") as configfile:
            config.write(configfile, space_around_delimiters=False)

        return mods_settings_path
