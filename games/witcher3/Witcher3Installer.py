"""
Witcher 3 Installer Helper for Mod Organizer 2

Standalone installer plugin that handles path inconsistencies in Witcher 3 mod archives
by detecting and correcting common problematic folder structures while preserving 
MO2's installation preview.
"""

import logging
import re
from pathlib import Path
from typing import List, Optional, Dict, Tuple, Union
from dataclasses import dataclass
from enum import Enum

import mobase

logger = logging.getLogger(__name__)


# ============================================================================
# Archive Analysis Classes
# ============================================================================

class ArchivePattern(Enum):
    """Enumeration of detected archive patterns"""
    IDEAL_STRUCTURE = "ideal"           # Mods/ and/or DLC/ at root
    DIRECT_MOD_DLC = "direct"          # mod#### or dlc#### at root
    SINGLE_WRAPPER = "single_wrapper"   # One wrapper folder containing Mods/DLC/
    NAMED_WRAPPER = "named_wrapper"     # ModName/ containing mod####/dlc#### directly
    MIXED_CONTENT = "mixed"            # Mix of correct and incorrect structure
    NESTED_STRUCTURE = "nested"        # Content/Mods/ or similar deep nesting
    UNKNOWN = "unknown"                # Unrecognized pattern


@dataclass
class ModFolder:
    """Represents a detected mod folder"""
    name: str
    path: str
    entry: mobase.IFileTree
    is_valid_mod: bool = False


@dataclass
class DLCFolder:
    """Represents a detected DLC folder"""
    name: str
    path: str
    entry: mobase.IFileTree
    is_valid_dlc: bool = False


@dataclass
class ArchiveAnalysis:
    """Analysis result of an archive's structure"""
    pattern: ArchivePattern = ArchivePattern.UNKNOWN
    mod_folders: List[ModFolder] = None
    dlc_folders: List[DLCFolder] = None
    wrapper_folders: List[str] = None
    needs_correction: bool = False
    correction_strategy: str = ""
    confidence: float = 0.0
    
    def __post_init__(self):
        if self.mod_folders is None:
            self.mod_folders = []
        if self.dlc_folders is None:
            self.dlc_folders = []
        if self.wrapper_folders is None:
            self.wrapper_folders = []


class PatternDetector:
    """Detects and analyzes Witcher 3 archive patterns"""
    
    # Flexible regex patterns for mod and DLC folder detection
    MOD_PATTERN = re.compile(r'^mod\d*.*', re.IGNORECASE)     # mod, mod1, mod0001, modIgni2013, etc.
    DLC_PATTERN = re.compile(r'^dlc\d*.*', re.IGNORECASE)     # dlc, dlc1, dlc01, dlcIgni2013, etc.
    
    # File extensions that indicate Witcher 3 content
    TW3_EXTENSIONS = {'.w3modscript', '.ws', '.bundle', '.w3strings', '.csv'}
    TW3_FILENAMES = {'modscripts', 'dlcscripts', 'gameplay', 'content'}
    
    # The game's own top-level folders. "Mods" also matches MOD_PATTERN, so
    # these must be excluded explicitly or a wrapper holding Mods/ would be
    # "fixed" into Mods/Mods/.
    GAME_ROOT_FOLDERS = {'mods', 'dlc'}

    @classmethod
    def is_game_root_folder(cls, folder_name: str) -> bool:
        """Check if folder is the game's Mods/ or DLC/ folder"""
        return folder_name.lower() in cls.GAME_ROOT_FOLDERS

    @classmethod
    def is_mod_folder(cls, folder_name: str) -> bool:
        """Check if folder name matches mod pattern"""
        result = bool(cls.MOD_PATTERN.match(folder_name)) and not cls.is_game_root_folder(folder_name)
        logger.debug(f"Checking mod pattern for '{folder_name}': {result}")
        return result

    @classmethod
    def is_dlc_folder(cls, folder_name: str) -> bool:
        """Check if folder name matches DLC pattern"""
        result = bool(cls.DLC_PATTERN.match(folder_name)) and not cls.is_game_root_folder(folder_name)
        logger.debug(f"Checking DLC pattern for '{folder_name}': {result}")
        return result

    @classmethod
    def is_wrapper_folder(cls, entry: mobase.FileTreeEntry) -> bool:
        """Check if a folder wraps mod/dlc folders or Mods/ / DLC/ one level down.

        A real modXXX folder holds content/, so a mod-named folder that itself
        holds mod/dlc folders (e.g. Mod_Pack/modA) is a wrapper, not a mod.
        """
        if not entry.isDir() or cls.is_game_root_folder(entry.name()):
            return False
        return any(
            sub.isDir() and (
                cls.is_mod_folder(sub.name())
                or cls.is_dlc_folder(sub.name())
                or cls.is_game_root_folder(sub.name())
            )
            for sub in entry
        )

    @classmethod
    def is_loose_mod_or_dlc(cls, entry: mobase.FileTreeEntry) -> bool:
        """Check if entry is a modXXX/dlcXXX folder outside Mods/ / DLC/"""
        return (
            entry.isDir()
            and (cls.is_mod_folder(entry.name()) or cls.is_dlc_folder(entry.name()))
            and not cls.is_wrapper_folder(entry)
        )
    
    @classmethod
    def has_tw3_content(cls, tree: mobase.IFileTree) -> bool:
        """Check if tree contains Witcher 3 specific content"""
        for entry in tree:
            if entry.isFile():
                # Check file extensions
                if any(entry.name().lower().endswith(ext) for ext in cls.TW3_EXTENSIONS):
                    return True
                # Check specific filenames
                if any(name in entry.name().lower() for name in cls.TW3_FILENAMES):
                    return True
            elif entry.isDir():
                # Recursively check subdirectories
                if cls.has_tw3_content(entry):
                    return True
        return False


# ============================================================================
# Main Installer Plugin
# ============================================================================

class Witcher3Installer(mobase.IPluginInstallerSimple):
    """Witcher 3 mod installer that fixes common archive structure issues"""
    
    def __init__(self):
        super().__init__()
        self._detector = PatternDetector()
        self._logger = logging.getLogger("Witcher3Installer")
        self._logger.setLevel(logging.DEBUG)
        self._logger.info("Witcher 3 Installer initialized")
        
        # Test the patterns during initialization
        test_names = ["mod0001_TestMod", "dlc01_TestDLC", "modIgni2013", "dlcIgni2013"]
        for name in test_names:
            self._logger.debug(f"Pattern test - {name}: mod={self._detector.is_mod_folder(name)}, dlc={self._detector.is_dlc_folder(name)}")
    
    def init(self, organizer: mobase.IOrganizer) -> bool:
        """Initialize the installer plugin"""
        self._organizer = organizer
        return True
    
    def name(self) -> str:
        return "Witcher 3 Installer Helper"
    
    def localizedName(self) -> str:
        return "Witcher 3 Installer Helper"
    
    def author(self) -> str:
        return "Evannee45"
    
    def description(self) -> str:
        return "Fixes common path structure issues in Witcher 3 mod archives"
    
    def version(self) -> mobase.VersionInfo:
        return mobase.VersionInfo(1, 1, 0)
    
    def settings(self) -> List[mobase.PluginSetting]:
        return [
            mobase.PluginSetting("enabled", "Enable Witcher 3 installer helper", True),
            mobase.PluginSetting("auto_fix", "Automatically fix detected issues", True)
        ]
    
    def isActive(self) -> bool:
        return True
    
    def priority(self) -> int:
        """Return installer priority - lower than manual installers to allow preview"""
        return 10
    
    def isManualInstaller(self) -> bool:
        """This is not a manual installer"""
        return False
    
    def _is_witcher3_managed(self) -> bool:
        """Only act when MO2 is managing The Witcher 3"""
        try:
            return self._organizer.managedGame().gameShortName() == "witcher3"
        except Exception:
            return False

    def isArchiveSupported(self, tree: mobase.IFileTree) -> bool:
        """Check if this archive contains Witcher 3 content that needs fixing"""
        try:
            if not self._is_witcher3_managed():
                return False

            self._logger.info("=== CHECKING ARCHIVE SUPPORT ===")

            has_game_root_folder = False
            found_mod_dlc_direct = False
            found_wrapper = False

            for entry in tree:
                entry_name = entry.name()
                self._logger.debug(f"  ROOT: {entry_name} (dir: {entry.isDir()})")

                if not entry.isDir():
                    continue

                # Check for ideal structure (Mods/ or DLC/ folders)
                if self._detector.is_game_root_folder(entry_name):
                    has_game_root_folder = True
                    self._logger.info(f"  -> FOUND {entry_name.upper()} FOLDER")

                # Check for wrapper folders (like ModName/mod#### or ModName/Mods/)
                elif self._detector.is_wrapper_folder(entry):
                    self._logger.info(f"  -> FOUND WRAPPER FOLDER: {entry_name}")
                    found_wrapper = True

                # Check for direct mod/dlc folders at root
                elif self._detector.is_loose_mod_or_dlc(entry):
                    self._logger.info(f"  -> FOUND DIRECT MOD/DLC FOLDER: {entry_name}")
                    found_mod_dlc_direct = True

            # If already has ideal structure, let MO2 handle it
            if has_game_root_folder:
                self._logger.info("Archive has ideal structure - not supporting")
                return False

            # Support if we found mod/dlc folders (direct or wrapped)
            needs_support = found_mod_dlc_direct or found_wrapper
            self._logger.info(f"Archive supported: {needs_support} (direct: {found_mod_dlc_direct}, wrapper: {found_wrapper})")
            return needs_support

        except Exception as e:
            self._logger.error(f"Error analyzing archive: {e}")
            return False
    
    def install(self, name: mobase.GuessedString, tree: mobase.IFileTree, 
                version: str, nexus_id: int) -> Union[mobase.InstallResult, mobase.IFileTree]:
        """Install the mod with structure corrections"""
        try:
            self._logger.info("=== WITCHER 3 INSTALLER CALLED ===")
            self._logger.info(f"Mod name: {name}")
            
            # Check what type of correction is needed
            needs_direct_correction = False
            wrapper_folders_with_mods = []
            
            if not self._is_witcher3_managed():
                return mobase.InstallResult.NOT_ATTEMPTED

            # Check root level for direct mod/dlc folders
            for entry in tree:
                if self._detector.is_loose_mod_or_dlc(entry):
                    self._logger.info(f"Found direct folder needing correction: {entry.name()}")
                    needs_direct_correction = True
                    break

            # Check for wrapper folders containing mod/dlc or Mods/ / DLC/
            if not needs_direct_correction:
                for entry in tree:
                    if self._detector.is_wrapper_folder(entry):
                        wrapper_folders_with_mods.append(entry.name())
                        self._logger.info(f"Found wrapper folder with mods: {entry.name()}")
            
            # Check for multiple wrapper folders - let user handle manually
            if len(wrapper_folders_with_mods) > 1:
                self._logger.info(f"Multiple wrapper folders detected: {wrapper_folders_with_mods}")
                self._logger.info("Multiple options detected - letting user choose manually")
                return mobase.InstallResult.NOT_ATTEMPTED
            
            # No correction needed
            if not needs_direct_correction and len(wrapper_folders_with_mods) == 0:
                self._logger.info("No correction needed - letting MO2 handle")
                return mobase.InstallResult.NOT_ATTEMPTED
            
            # Apply structure correction for simple cases only
            if needs_direct_correction or len(wrapper_folders_with_mods) == 1:
                self._logger.info("Applying corrections to original tree...")
                self._apply_corrections_in_place(tree)
                
                # Return NOT_ATTEMPTED so MO2 shows the preview with our corrected structure
                self._logger.info("Corrections applied - letting MO2 show preview")
                return mobase.InstallResult.NOT_ATTEMPTED
            
            return mobase.InstallResult.NOT_ATTEMPTED
                
        except Exception as e:
            self._logger.error(f"Installation failed: {e}")
            import traceback
            self._logger.error(traceback.format_exc())
            return mobase.InstallResult.FAILED
    
    def _apply_corrections_in_place(self, tree: mobase.IFileTree):
        """Apply corrections directly to the original tree, then let MO2 show preview"""
        self._logger.info("Modifying original tree structure in-place")
        
        # Check what content we actually have
        has_mod_content = False
        has_dlc_content = False
        
        # First pass: determine what types of content we have. A wrapper's
        # own Mods/ or DLC/ is merged to the root as-is, so only loose
        # mod/dlc folders need a target folder created for them.
        for entry in list(tree):
            if self._detector.is_loose_mod_or_dlc(entry):
                if self._detector.is_mod_folder(entry.name()):
                    has_mod_content = True
                else:
                    has_dlc_content = True
            elif self._detector.is_wrapper_folder(entry):
                for sub_entry in entry:
                    if self._detector.is_loose_mod_or_dlc(sub_entry):
                        if self._detector.is_mod_folder(sub_entry.name()):
                            has_mod_content = True
                        else:
                            has_dlc_content = True
        
        # Find existing Mods and DLC directories
        mods_dir = None
        dlc_dir = None
        
        for entry in tree:
            if entry.isDir() and entry.name().lower() == "mods":
                mods_dir = entry
            elif entry.isDir() and entry.name().lower() == "dlc":
                dlc_dir = entry
        
        # Create directories only if we have content for them
        if has_mod_content and not mods_dir:
            mods_dir = tree.addDirectory("Mods")
            self._logger.info("Created Mods/ directory")
        
        if has_dlc_content and not dlc_dir:
            dlc_dir = tree.addDirectory("DLC")
            self._logger.info("Created DLC/ directory")
        
        # Handle direct mod/dlc folders at root level first
        entries_to_move = []
        for entry in list(tree):
            if self._detector.is_loose_mod_or_dlc(entry):
                name = entry.name()
                if self._detector.is_mod_folder(name):
                    self._logger.info(f"Will move direct mod folder {name} to Mods/")
                    entries_to_move.append((entry, "Mods/"))
                else:
                    self._logger.info(f"Will move direct DLC folder {name} to DLC/")
                    entries_to_move.append((entry, "DLC/"))

        # Move direct entries
        for entry, destination in entries_to_move:
            tree.move(entry, destination, mobase.IFileTree.InsertPolicy.REPLACE)

        # Handle wrapper folders containing mod/dlc or Mods/ / DLC/
        wrapper_entries_to_process = [
            entry for entry in list(tree) if self._detector.is_wrapper_folder(entry)
        ]
        
        # Process wrapper folders
        for wrapper_entry in wrapper_entries_to_process:
            self._logger.info(f"Processing wrapper folder {wrapper_entry.name()}")
            self._process_wrapper_folder(tree, wrapper_entry)
        
        self._logger.info("In-place corrections completed")
    
    def _process_wrapper_folder(self, tree: mobase.IFileTree, wrapper_entry: mobase.IFileTree):
        """Process a wrapper folder and move its contents appropriately"""
        self._logger.info(f"Processing wrapper folder: {wrapper_entry.name()}")
        
        # Collect sub-entries to move
        sub_entries_to_move = []
        
        for sub_entry in list(wrapper_entry):
            if self._detector.is_loose_mod_or_dlc(sub_entry):
                if self._detector.is_mod_folder(sub_entry.name()):
                    self._logger.info(f"Will move nested mod folder {sub_entry.name()} to Mods/")
                    sub_entries_to_move.append((sub_entry, "Mods/"))
                else:
                    self._logger.info(f"Will move nested DLC folder {sub_entry.name()} to DLC/")
                    sub_entries_to_move.append((sub_entry, "DLC/"))
            else:
                # Move files and other folders (including a nested Mods/ or
                # DLC/, which merges with any existing one) up to root
                sub_entries_to_move.append((sub_entry, "./"))

        # Move all sub-entries; folders merge so existing Mods/ / DLC/ keep their content
        for sub_entry, destination in sub_entries_to_move:
            policy = (
                mobase.IFileTree.InsertPolicy.MERGE
                if sub_entry.isDir()
                else mobase.IFileTree.InsertPolicy.REPLACE
            )
            tree.move(sub_entry, destination, policy)
        
        # Remove empty wrapper folder
        if not list(wrapper_entry):
            tree.remove(wrapper_entry)


# ============================================================================
# Plugin Creation Function
# ============================================================================

def createPlugin() -> mobase.IPlugin:
    """Create the Witcher 3 installer plugin"""
    return Witcher3Installer()