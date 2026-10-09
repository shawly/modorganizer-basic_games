"""
Witcher 3 Support Plugin for Mod Organizer 2

Provides game support and integrated mod sorting functionality.
"""

import configparser
import json
import logging
from enum import Enum
from pathlib import Path
from typing import List, Optional

import mobase
from PyQt6.QtCore import QDir, QFileInfo, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTreeWidget, QTreeWidgetItem,
    QHeaderView, QMenu, QPushButton, QLabel, QAbstractItemView, QMainWindow,
    QTabWidget, QMessageBox
)

from ..basic_features import BasicGameSaveGameInfo
from ..basic_features.basic_save_game_info import BasicGameSaveGame
from ..basic_game import BasicGame

logger = logging.getLogger(__name__)


# ============================================================================
# Mod Settings Management
# ============================================================================

class ModEntry:
    """Represents a single mod entry in mods.settings"""

    def __init__(self, name: str, enabled: bool = True, priority: int = 0):
        self.name = name
        self.enabled = enabled
        self.priority = priority

    def __str__(self):
        return f"ModEntry(name='{self.name}', enabled={self.enabled}, priority={self.priority})"

class ModsSettingsManager:
    """Manages the mods.settings file for Witcher 3"""

    def __init__(self, mods_settings_path: str):
        self.mods_settings_path = Path(mods_settings_path)
        self.mod_entries: List[ModEntry] = []

    def load_mods_settings(self) -> bool:
        """Load mod entries from mods.settings file"""
        try:
            if not self.mods_settings_path.exists():
                logger.info("mods.settings not found, creating empty list")
                self.mod_entries = []
                return True

            config = configparser.ConfigParser()
            config.read(self.mods_settings_path, encoding='utf-8')

            self.mod_entries = []

            for section_name in config.sections():
                enabled = config.getboolean(section_name, 'Enabled', fallback=True)
                priority = config.getint(section_name, 'Priority', fallback=len(self.mod_entries))

                self.mod_entries.append(ModEntry(section_name, enabled, priority))

            # Sort by priority
            self.mod_entries.sort(key=lambda x: x.priority)
            logger.info(f"Loaded {len(self.mod_entries)} mod entries from mods.settings")
            return True

        except Exception as e:
            logger.error(f"Error loading mods.settings: {e}")
            self.mod_entries = []
            return False

    def save_mods_settings(self) -> bool:
        """Save current mod entries to mods.settings file"""
        try:
            # Ensure directory exists
            self.mods_settings_path.parent.mkdir(parents=True, exist_ok=True)

            config = configparser.ConfigParser()
            # Keep key case as written: configparser lowercases keys by default,
            # and Script Merger only accepts exactly "Enabled=" / "Priority="
            config.optionxform = str

            # Add each mod entry as a section
            for mod_entry in self.mod_entries:
                config.add_section(mod_entry.name)
                config.set(mod_entry.name, 'Enabled', '1' if mod_entry.enabled else '0')
                config.set(mod_entry.name, 'Priority', str(mod_entry.priority))

            # Write to file
            with open(self.mods_settings_path, 'w', encoding='utf-8') as configfile:
                config.write(configfile, space_around_delimiters=False)

            logger.debug(f"Saved {len(self.mod_entries)} mod entries to mods.settings")
            return True

        except Exception as e:
            logger.error(f"Error saving mods.settings: {e}")
            return False

    def sync_with_mod_list(self, mod_list: List[str]):
        """Sync entries with a list of mod names, preserving existing order"""
        # Create map of existing entries
        existing_entries = {entry.name: entry for entry in self.mod_entries}

        # Create new entries list
        new_entries = []
        for i, mod_name in enumerate(mod_list):
            if mod_name in existing_entries:
                # Keep existing entry but update priority
                entry = existing_entries[mod_name]
                entry.priority = i
                new_entries.append(entry)
            else:
                # Create new entry
                new_entries.append(ModEntry(mod_name, enabled=True, priority=i))

        self.mod_entries = new_entries

# ============================================================================
# UI Components
# ============================================================================

class ModOrderTree(QTreeWidget):
    """Mod order list: checkbox, Priority, mod name, and source MO2 mod.

    Multi-selectable and drag-reorderable (mirrors the KCD2 plugin's Load
    Order tab). The Priority column numbers rows top-to-bottom starting
    at 1, matching the Priority value written to mods.settings (row
    index, ascending from the top).

    Unlike most other games, a LOWER Priority number in Witcher 3's
    mods.settings wins: that mod is applied last and overwrites the
    others. So the mod at the TOP of this list (Priority 1) is Witcher
    3's highest-priority/winning mod, and priority decreases going down.
    """

    COL_CHECK = 0
    COL_INDEX = 1
    COL_NAME = 2
    COL_SOURCE = 3

    orderChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.organizer = None
        self.mod_folder_to_mo2_mod = {}
        self.parent_widget = None

        self.setColumnCount(4)
        self.setHeaderLabels(["", "Priority", "Mod", "Source Mod"])
        self.setRootIsDecorated(False)
        self.setItemsExpandable(False)
        self.setUniformRowHeights(True)
        self.setAlternatingRowColors(True)

        # Multi-select + drag-and-drop reordering
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDropIndicatorShown(True)

        header = self.header()
        header.setSectionResizeMode(self.COL_CHECK, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.COL_INDEX, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.COL_NAME, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(self.COL_SOURCE, QHeaderView.ResizeMode.ResizeToContents)

        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.itemChanged.connect(self.on_item_changed)

    def set_organizer(self, organizer):
        self.organizer = organizer

    def set_mod_mapping(self, mapping):
        self.mod_folder_to_mo2_mod = mapping

    def set_parent_widget(self, parent_widget):
        self.parent_widget = parent_widget

    def on_item_changed(self, item, column):
        """Handle checkbox state changes"""
        if column != self.COL_CHECK:
            return
        if self.parent_widget and self.parent_widget.sync_enabled:
            self.parent_widget.sync_to_settings()

    def _restore_moved_order(self, moved_order_before, selected_ids: set) -> None:
        """Fix up the relative order of a just-dropped multi-row block.

        Qt's default ``QAbstractItemView`` internal-move drop handling
        correctly figures out where to insert (every row here has
        ``ItemIsDropEnabled`` cleared, so drops can't land "on" a row and
        reparent it as a hidden child). Its one remaining defect:
        dragging more than one row together reverses their relative
        order within the moved block -- a long-documented Qt behavior.
        Called after ``super().dropEvent()``: finds that contiguous
        block (by identity, via ``selected_ids``) and reinserts it in
        ``moved_order_before``'s order, in place.
        """
        if len(moved_order_before) <= 1:
            return
        all_after = [self.topLevelItem(i) for i in range(self.topLevelItemCount())]
        positions = sorted(
            i for i, it in enumerate(all_after) if id(it) in selected_ids
        )
        contiguous = positions and positions == list(
            range(positions[0], positions[0] + len(positions))
        )
        if not contiguous:
            return
        start = positions[0]
        self.blockSignals(True)
        for _ in positions:
            self.takeTopLevelItem(start)
        for offset, it in enumerate(moved_order_before):
            self.insertTopLevelItem(start + offset, it)
            it.setSelected(True)
        self.blockSignals(False)

    def dropEvent(self, event):
        """Handle drop events - trigger renumber + sync after reorder"""
        if event.source() != self:
            event.ignore()
            return

        selected_ids = {id(it) for it in self.selectedItems()}
        all_before = [self.topLevelItem(i) for i in range(self.topLevelItemCount())]
        moved_order_before = [it for it in all_before if id(it) in selected_ids]

        super().dropEvent(event)
        self._restore_moved_order(moved_order_before, selected_ids)

        self.renumber()
        self.orderChanged.emit()
        if self.parent_widget and self.parent_widget.sync_enabled:
            logger.info("Drag-and-drop completed, triggering sync")
            self.parent_widget.sync_to_settings()

    def add_mod_item(self, mod_name: str, enabled: bool = True, tooltip: Optional[str] = None) -> QTreeWidgetItem:
        """Add a mod row with checkbox, source-mod column, and tooltip."""
        item = QTreeWidgetItem()
        item.setText(self.COL_NAME, mod_name)
        item.setFlags(
            (
                item.flags()
                | Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsDragEnabled
            )
            # QTreeWidgetItem is drop-enabled by default, which lets a
            # drop land "on" another row and reparent it as a hidden
            # child instead of reordering it as a sibling -- the row
            # then vanishes from every topLevelItem() scan. Clearing it
            # forces drops to only ever land *between* rows.
            & ~Qt.ItemFlag.ItemIsDropEnabled
        )
        item.setCheckState(
            self.COL_CHECK, Qt.CheckState.Checked if enabled else Qt.CheckState.Unchecked
        )

        mo2_mod_name = self.mod_folder_to_mo2_mod.get(mod_name)
        item.setText(self.COL_SOURCE, mo2_mod_name if mo2_mod_name else "Unknown")
        item.setToolTip(self.COL_NAME, tooltip or mo2_mod_name or "Unknown MO2 Mod")

        if not enabled:
            for col in range(4):
                item.setForeground(col, Qt.GlobalColor.gray)

        self.addTopLevelItem(item)
        return item

    def renumber(self):
        """Refresh the Priority column: top-to-bottom, starting at 1.

        This is the row's position (1-based), matching the Priority
        value written to mods.settings (row index, 0-based, ascending
        from the top) plus one. Witcher 3 treats a LOWER Priority number
        as higher priority -- that mod is applied last and overwrites
        the others -- opposite of most other games, so Priority 1 (the
        top of the list) is the winning mod, not the weakest one.
        """
        for i in range(self.topLevelItemCount()):
            item = self.topLevelItem(i)
            if item:
                item.setText(self.COL_INDEX, str(i + 1))

    def get_mod_names_in_order(self) -> List[str]:
        """Get list of all mod names in current order"""
        mod_names = []
        for i in range(self.topLevelItemCount()):
            item = self.topLevelItem(i)
            if item:
                mod_names.append(item.text(self.COL_NAME))
        return mod_names

    def get_mods_with_enabled_state(self) -> List[tuple]:
        """Get list of (mod_name, enabled) tuples in current order"""
        mods = []
        for i in range(self.topLevelItemCount()):
            item = self.topLevelItem(i)
            if item:
                mod_name = item.text(self.COL_NAME)
                enabled = item.checkState(self.COL_CHECK) == Qt.CheckState.Checked
                mods.append((mod_name, enabled))
        return mods

class ModSortingWidget(QWidget):
    """Main mod sorting widget"""

    def __init__(self, organizer, parent=None):
        super().__init__(parent)
        self.organizer = organizer
        self.settings_manager: Optional[ModsSettingsManager] = None
        self.sync_timer = None

        # Load sync state from file (defaults to True)
        self.sync_enabled = self.load_sync_state()

        self.setup_ui()
        self.initialize()
        self.connect_mo2_events()

    def setup_ui(self):
        """Set up the user interface"""
        layout = QVBoxLayout(self)

        # Header
        header_layout = QHBoxLayout()
        title_label = QLabel("Mod Load Order")
        title_label.setStyleSheet("font-weight: bold; font-size: 14px; margin: 5px;")
        header_layout.addWidget(title_label)
        header_layout.addStretch()

        self.enable_all_btn = QPushButton("Enable All")
        self.enable_all_btn.setToolTip("Enable all mods")
        self.enable_all_btn.clicked.connect(self.enable_all_mods)
        header_layout.addWidget(self.enable_all_btn)

        self.disable_all_btn = QPushButton("Disable All")
        self.disable_all_btn.setToolTip("Disable all mods")
        self.disable_all_btn.clicked.connect(self.disable_all_mods)
        header_layout.addWidget(self.disable_all_btn)

        self.top_btn = QPushButton("Send to Top")
        self.top_btn.setToolTip("Move selected mod(s) to the top of the list")
        self.top_btn.clicked.connect(lambda: self.send_selected(top=True))
        header_layout.addWidget(self.top_btn)

        self.bottom_btn = QPushButton("Send to Bottom")
        self.bottom_btn.setToolTip("Move selected mod(s) to the bottom of the list")
        self.bottom_btn.clicked.connect(lambda: self.send_selected(top=False))
        header_layout.addWidget(self.bottom_btn)

        self.sort_btn = QPushButton("A-Z")
        self.sort_btn.setToolTip("Sort mods alphabetically")
        self.sort_btn.clicked.connect(self.sort_alphabetically)
        header_layout.addWidget(self.sort_btn)

        self.modlist_sort_btn = QPushButton("Modlist Order")
        self.modlist_sort_btn.setToolTip("MO2's winning (bottom-most) mod becomes the top (highest priority) mod here\nMods from same folder sorted alphabetically")
        self.modlist_sort_btn.clicked.connect(self.sort_by_modlist_order)
        header_layout.addWidget(self.modlist_sort_btn)

        self.sync_btn = QPushButton("Sync")
        self.sync_btn.setToolTip("Refresh mod list and sync to mods.settings")
        self.sync_btn.clicked.connect(self.manual_refresh)
        header_layout.addWidget(self.sync_btn)

        self.sync_toggle_btn = QPushButton("Sync Enabled")
        self.sync_toggle_btn.setToolTip("Toggle automatic syncing to mods.settings\nDisable to manually edit mods.settings file")
        self.sync_toggle_btn.setCheckable(True)
        self.sync_toggle_btn.setChecked(True)
        self.sync_toggle_btn.clicked.connect(self.toggle_sync)
        self.update_sync_button_style()
        header_layout.addWidget(self.sync_toggle_btn)

        layout.addLayout(header_layout)

        # Instructions
        instruction_label = QLabel(
            "Drag and drop to reorder mods (multi-select supported). "
            "Check/uncheck to enable/disable. Top of the list = highest "
            "priority (Witcher 3 applies it last, so it overwrites the mods "
            "below it)."
        )
        instruction_label.setStyleSheet("color: gray; margin: 5px;")
        layout.addWidget(instruction_label)

        # Mod list
        self.mod_list = ModOrderTree()
        self.mod_list.set_organizer(self.organizer)
        self.mod_list.set_parent_widget(self)
        self.mod_list.customContextMenuRequested.connect(self.show_context_menu)
        layout.addWidget(self.mod_list)

        # Status label
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("margin: 5px;")
        layout.addWidget(self.status_label)

    def initialize(self):
        """Initialize settings manager and load mods"""
        try:
            # Determine the correct path for mods.settings
            mods_settings_path = self.get_mods_settings_path()
            logger.info(f"Initializing Witcher 3 mod management with mods.settings at: {mods_settings_path}")

            self.settings_manager = ModsSettingsManager(mods_settings_path)
            self.refresh_mod_list()

        except Exception as e:
            logger.error(f"Error initializing: {e}")

    def get_mods_settings_path(self) -> str:
        """Get the correct path for mods.settings based on MO2 profile settings"""
        try:
            # Use the direct profile path from MO2 - this should be the current active profile
            current_profile_path = Path(self.organizer.profilePath())

            # Check if profile-specific INI files are enabled by looking for settings files
            profile_specific_ini = False

            if current_profile_path.exists():
                profile_ini_files = list(current_profile_path.glob("*.settings"))
                has_profile_settings = len(profile_ini_files) > 0

                # If there are .settings files in the profile, assume profile-specific is enabled
                profile_specific_ini = has_profile_settings
            else:
                logger.warning(f"Profile path does not exist: {current_profile_path}")

            # Also try the API method if available
            if hasattr(self.organizer, 'profileSettingsBool'):
                try:
                    api_local_settings = self.organizer.profileSettingsBool("LocalSettingsEnabled")
                    # Use API result if file detection didn't find anything
                    if not profile_specific_ini:
                        profile_specific_ini = api_local_settings
                except Exception:
                    pass  # API method not available or failed

            if profile_specific_ini:
                # Use current profile directory for mods.settings
                mods_settings_path = current_profile_path / "mods.settings"
                logger.info(f"Using profile-specific mods.settings: {mods_settings_path}")
            else:
                # Use global documents directory for mods.settings
                game_docs = self.organizer.managedGame().documentsDirectory().absolutePath()
                mods_settings_path = Path(game_docs) / "mods.settings"
                logger.info(f"Using global mods.settings: {mods_settings_path}")

            return str(mods_settings_path)

        except Exception as e:
            # Fallback to global documents directory
            logger.warning(f"Error determining mods.settings path, using global fallback: {e}")
            game_docs = self.organizer.managedGame().documentsDirectory().absolutePath()
            return str(Path(game_docs) / "mods.settings")

    def connect_mo2_events(self):
        """Connect to MO2 events for real-time monitoring"""
        try:
            mod_list = self.organizer.modList()
            if hasattr(mod_list, 'modStateChanged'):
                mod_list.modStateChanged.connect(self.on_mod_state_changed)
                logger.info("Connected to modStateChanged event")
            else:
                logger.warning("modStateChanged event not available")

            # Try to connect to profile change event
            if hasattr(self.organizer, 'profileChanged'):
                try:
                    self.organizer.profileChanged.connect(self.on_profile_changed)
                    logger.info("Connected to profileChanged event")
                except Exception as e:
                    logger.warning(f"Failed to connect to profileChanged: {e}")
            else:
                logger.warning("profileChanged event not available")

            # Store current profile path for manual checks
            self.current_profile_path = str(self.get_mods_settings_path())

            # Only setup fallback for modlist monitoring, not profile monitoring
            self.setup_fallback_monitoring()

        except Exception as e:
            logger.warning(f"Could not connect to MO2 events: {e}")
            self.current_profile_path = str(self.get_mods_settings_path())
            self.setup_fallback_monitoring()

    def load_sync_state(self) -> bool:
        """Load sync state from file adjacent to plugin"""
        try:
            plugin_dir = Path(__file__).parent
            sync_state_file = plugin_dir / "witcher3_sync_state.txt"

            if sync_state_file.exists():
                content = sync_state_file.read_text(encoding='utf-8').strip().lower()
                enabled = content == "enabled"
                logger.info(f"Loaded sync state: {enabled}")
                return enabled
            else:
                logger.info("No sync state file found, defaulting to enabled")
                return True

        except Exception as e:
            logger.warning(f"Failed to load sync state, defaulting to enabled: {e}")
            return True

    def save_sync_state(self):
        """Save sync state to file adjacent to plugin"""
        try:
            plugin_dir = Path(__file__).parent
            sync_state_file = plugin_dir / "witcher3_sync_state.txt"

            content = "enabled" if self.sync_enabled else "disabled"
            sync_state_file.write_text(content, encoding='utf-8')
            logger.debug(f"Saved sync state: {content}")

        except Exception as e:
            logger.warning(f"Failed to save sync state: {e}")

    def setup_fallback_monitoring(self):
        """Setup fallback timer-based monitoring"""
        logger.info("Setting up fallback modlist.txt monitoring")
        self.fallback_timer = QTimer()
        self.fallback_timer.timeout.connect(self.check_mod_changes)
        self.fallback_timer.start(2000)  # Check every 2 seconds

        try:
            modlist_path = Path(self.organizer.profilePath()) / "modlist.txt"
            if modlist_path.exists():
                self.last_modlist_mtime = modlist_path.stat().st_mtime
                logger.info(f"Initialized modlist.txt monitoring: {modlist_path}")
            else:
                self.last_modlist_mtime = 0
                logger.warning(f"modlist.txt not found: {modlist_path}")
        except Exception as e:
            logger.error(f"Failed to initialize modlist monitoring: {e}")
            self.last_modlist_mtime = 0

    def on_mod_state_changed(self, mod_name, state):
        """Handle MO2 mod state change"""
        logger.info(f"MO2 mod state changed: {mod_name} -> {state}")
        self.full_sync()

    def on_profile_changed(self):
        """Handle MO2 profile change"""
        logger.info("Profile changed, reinitializing settings manager and triggering full sync")

        # Reinitialize settings manager with potentially new path
        try:
            mods_settings_path = self.get_mods_settings_path()
            self.settings_manager = ModsSettingsManager(mods_settings_path)
            logger.info(f"Reinitialized settings manager for new profile: {mods_settings_path}")

            # Force reload settings from the new path
            self.settings_manager.load_mods_settings()

        except Exception as e:
            logger.error(f"Error reinitializing settings manager on profile change: {e}")

        self.full_sync()

    def check_mod_changes(self):
        """Fallback check for mod changes by monitoring modlist.txt"""
        try:
            modlist_path = Path(self.organizer.profilePath()) / "modlist.txt"
            if modlist_path.exists():
                current_mtime = modlist_path.stat().st_mtime
                if hasattr(self, 'last_modlist_mtime') and current_mtime != self.last_modlist_mtime:
                    logger.info("Detected modlist.txt change via fallback monitor")
                    self.last_modlist_mtime = current_mtime
                    self.full_sync()
                elif not hasattr(self, 'last_modlist_mtime'):
                    self.last_modlist_mtime = current_mtime
        except Exception as e:
            logger.error(f"Error in fallback mod check: {e}")

    def check_and_update_profile(self):
        """Check if profile has changed and update settings manager if needed"""
        try:
            current_settings_path = str(self.get_mods_settings_path())
            if hasattr(self, 'current_profile_path') and current_settings_path != self.current_profile_path:
                logger.info(f"Profile change detected - updating settings path")

                # Update stored path
                self.current_profile_path = current_settings_path

                # Reinitialize settings manager
                self.settings_manager = ModsSettingsManager(current_settings_path)
                logger.info(f"Reinitialized settings manager: {current_settings_path}")

                # Force reload settings from the new path
                self.settings_manager.load_mods_settings()

                # Refresh the UI with new profile's data
                self.full_sync()

                return True  # Profile changed
            return False  # No change
        except Exception as e:
            logger.error(f"Error checking profile change: {e}")
            return False

    def toggle_sync(self):
        """Toggle automatic syncing to mods.settings"""
        self.sync_enabled = self.sync_toggle_btn.isChecked()
        self.update_sync_button_style()

        # Save the sync state to file
        self.save_sync_state()

        if self.sync_enabled:
            logger.info("Automatic sync enabled - changes will be saved to mods.settings")
            # Perform a sync now that it's re-enabled
            self.sync_to_settings()
        else:
            logger.info("Automatic sync disabled - mods.settings will not be modified")

        # Update instruction text
        instruction_text = (
            "Drag and drop to reorder mods (multi-select supported). "
            "Check/uncheck to enable/disable. Top of the list = highest "
            "priority (Witcher 3 applies it last, so it overwrites the mods "
            "below it)."
        )
        if not self.sync_enabled:
            instruction_text += "\n⚠️ Sync disabled - changes won't be saved to mods.settings"

        # Find and update instruction label
        for i in range(self.layout().count()):
            widget = self.layout().itemAt(i).widget()
            if isinstance(widget, QLabel) and "Drag and drop" in widget.text():
                widget.setText(instruction_text)
                break

    def update_sync_button_style(self):
        """Update the sync button appearance based on state"""
        if self.sync_enabled:
            self.sync_toggle_btn.setText("Sync Enabled")
            self.sync_toggle_btn.setStyleSheet("QPushButton { background-color: #4CAF50; color: white; }")
            self.sync_btn.setEnabled(True)
        else:
            self.sync_toggle_btn.setText("Sync Disabled")
            self.sync_toggle_btn.setStyleSheet("QPushButton { background-color: #f44336; color: white; }")
            self.sync_btn.setEnabled(False)

    def _rebuild_in_order(self, ordered_mods: List[str]):
        """Clear and repopulate the tree in ``ordered_mods`` order.

        Preserves each mod's current enabled state and the existing
        mod_folder -> MO2 mod mapping.
        """
        enabled_by_name = dict(self.mod_list.get_mods_with_enabled_state())
        self.mod_list.clear()
        for mod_name in ordered_mods:
            self.mod_list.add_mod_item(mod_name, enabled_by_name.get(mod_name, True))
        self.mod_list.renumber()

    def send_selected(self, top: bool):
        """Move the selected mod(s) to the top or bottom of the list."""
        try:
            selected_names = {
                item.text(ModOrderTree.COL_NAME) for item in self.mod_list.selectedItems()
            }
            if not selected_names:
                return

            order = self.mod_list.get_mod_names_in_order()
            moved = [name for name in order if name in selected_names]
            remaining = [name for name in order if name not in selected_names]
            new_order = moved + remaining if top else remaining + moved

            self._rebuild_in_order(new_order)

            # Restore selection on the moved rows
            for i in range(self.mod_list.topLevelItemCount()):
                item = self.mod_list.topLevelItem(i)
                if item and item.text(ModOrderTree.COL_NAME) in selected_names:
                    item.setSelected(True)

            if self.sync_enabled:
                self.sync_to_settings()
        except Exception as e:
            logger.error(f"Error sending selected mods to {'top' if top else 'bottom'}: {e}")

    def show_context_menu(self, pos):
        """Right-click menu for the mod list (multi-select aware)."""
        if not self.mod_list.selectedItems():
            return
        menu = QMenu(self)
        menu.addAction("Enable Selected", lambda: self._set_selected_enabled(True))
        menu.addAction("Disable Selected", lambda: self._set_selected_enabled(False))
        menu.addSeparator()
        menu.addAction("Send to Top", lambda: self.send_selected(top=True))
        menu.addAction("Send to Bottom", lambda: self.send_selected(top=False))
        menu.exec(self.mod_list.viewport().mapToGlobal(pos))

    def _set_selected_enabled(self, enabled: bool):
        """Enable/disable all currently-selected mods in one sync pass."""
        items = self.mod_list.selectedItems()
        if not items:
            return
        self.mod_list.blockSignals(True)
        try:
            for item in items:
                item.setCheckState(
                    ModOrderTree.COL_CHECK,
                    Qt.CheckState.Checked if enabled else Qt.CheckState.Unchecked,
                )
        finally:
            self.mod_list.blockSignals(False)
        if self.sync_enabled:
            self.sync_to_settings()

    def sort_alphabetically(self):
        """Sort mods alphabetically in the UI list"""
        try:
            # Show confirmation dialog
            reply = QMessageBox.question(
                self,
                "Confirm Alphabetical Sort",
                "This operation will sort all mods alphabetically.\n\nThis operation is irreversible.\n\nDo you want to continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )

            if reply != QMessageBox.StandardButton.Yes:
                return

            logger.info("Sorting mods alphabetically")

            ordered_mods = sorted(self.mod_list.get_mod_names_in_order(), key=str.lower)
            self._rebuild_in_order(ordered_mods)

            # If sync is enabled, save the new order
            if self.sync_enabled:
                self.sync_to_settings()

        except Exception as e:
            logger.error(f"Error sorting alphabetically: {e}")

    def sort_by_modlist_order(self):
        """Sort mods so MO2's winning (bottom-most) mod becomes Witcher 3's
        winning (top-most) mod, with alphabetical sorting for mods from
        the same MO2 mod folder."""
        try:
            # Show confirmation dialog
            reply = QMessageBox.question(
                self,
                "Confirm Modlist Order Sort",
                "This operation will sort mods based on MO2 modlist order: "
                "MO2's winning (bottom-most) mod becomes the top (highest "
                "priority) mod here.\n\n"
                "Mods from the same MO2 mod folder will be sorted alphabetically.\n\n"
                "This operation is irreversible.\n\n"
                "Do you want to continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )

            if reply != QMessageBox.StandardButton.Yes:
                return

            logger.info("Sorting mods by modlist order (MO2 winner -> top)")

            # Get current items with their MO2 mod associations
            mod_names = self.mod_list.get_mod_names_in_order()
            items_with_mo2_mod = [
                (mod_name, self.mod_list.mod_folder_to_mo2_mod.get(mod_name, "Unknown"))
                for mod_name in mod_names
            ]

            # Get enabled MO2 mods in order
            enabled_mo2_mods = self.get_enabled_mods()

            # modlist.txt is stored in reverse of MO2's UI: its FIRST line
            # is MO2's winner (the bottom-most mod in MO2's list). Witcher 3's
            # winner is the TOP of this list, so rank = line index and sort
            # ascending: MO2's winner (rank 0) lands on top.
            mo2_rank = {mo2_mod: i for i, mo2_mod in enumerate(enabled_mo2_mods)}

            # Overwrite always wins in MO2, so it goes above everything;
            # mods with no known source go to the bottom
            overwrite_rank = -1
            unknown_rank = len(enabled_mo2_mods)

            def sort_key(item_tuple):
                mod_name, mo2_mod = item_tuple
                if mo2_mod == "⚠️ OVERWRITE":
                    rank = overwrite_rank
                else:
                    rank = mo2_rank.get(mo2_mod, unknown_rank)
                # Mods from the same MO2 mod: alphabetical, A-Z
                return (rank, mod_name.lower())

            items_with_mo2_mod.sort(key=sort_key)

            ordered_mods = [mod_name for mod_name, _ in items_with_mo2_mod]
            self._rebuild_in_order(ordered_mods)

            # If sync is enabled, save the new order
            if self.sync_enabled:
                self.sync_to_settings()

            logger.info(f"Sorted {len(items_with_mo2_mod)} mods by modlist order")

        except Exception as e:
            logger.error(f"Error sorting by modlist order: {e}")

    def enable_all_mods(self):
        """Enable all mods in the list"""
        try:
            logger.info("Enabling all mods")

            self.mod_list.blockSignals(True)
            try:
                for i in range(self.mod_list.topLevelItemCount()):
                    item = self.mod_list.topLevelItem(i)
                    if item:
                        item.setCheckState(ModOrderTree.COL_CHECK, Qt.CheckState.Checked)
            finally:
                self.mod_list.blockSignals(False)

            # If sync is enabled, save the changes
            if self.sync_enabled:
                self.sync_to_settings()

        except Exception as e:
            logger.error(f"Error enabling all mods: {e}")

    def disable_all_mods(self):
        """Disable all mods in the list"""
        try:
            logger.info("Disabling all mods")

            self.mod_list.blockSignals(True)
            try:
                for i in range(self.mod_list.topLevelItemCount()):
                    item = self.mod_list.topLevelItem(i)
                    if item:
                        item.setCheckState(ModOrderTree.COL_CHECK, Qt.CheckState.Unchecked)
            finally:
                self.mod_list.blockSignals(False)

            # If sync is enabled, save the changes
            if self.sync_enabled:
                self.sync_to_settings()

        except Exception as e:
            logger.error(f"Error disabling all mods: {e}")

    def manual_refresh(self):
        """Manual refresh triggered by user"""
        logger.info("Manual refresh triggered")
        self.refresh_mod_list()
        if self.sync_enabled:
            self.sync_to_settings()

    def full_sync(self):
        """Perform full synchronization"""
        try:
            self.refresh_mod_list()
            if self.sync_enabled:
                self.sync_to_settings()
        except Exception as e:
            logger.error(f"Error during full sync: {e}")

    def refresh_mod_list(self):
        """Refresh the mod list from enabled MO2 mods"""
        try:
            # Check if profile changed before refreshing
            if self.check_and_update_profile():
                # Profile changed, full_sync was already called in check_and_update_profile
                return

            if not self.settings_manager:
                return

            # Get enabled MO2 mods
            enabled_mo2_mods = self.get_enabled_mods()
            witcher3_mods = []
            mo2_mods_path = Path(self.organizer.modsPath())
            mod_folder_to_mo2_mod = {}
            overwrite_mods = []

            # First, check Overwrite folder for Witcher 3 mods (highest priority)
            # Overwrite is at the same level as mods folder, not inside it
            overwrite_base_path = mo2_mods_path.parent / "overwrite"
            overwrite_path = overwrite_base_path / "Mods"

            logger.debug(f"Checking Overwrite at: {overwrite_path}")

            if overwrite_path.exists() and overwrite_path.is_dir():
                for subdir in overwrite_path.iterdir():
                    if subdir.is_dir() and not subdir.name.startswith('.'):
                        mod_folder_name = subdir.name
                        witcher3_mods.append(mod_folder_name)
                        mod_folder_to_mo2_mod[mod_folder_name] = "⚠️ OVERWRITE"
                        overwrite_mods.append(mod_folder_name)
                        logger.info(f"Found Witcher 3 mod in Overwrite: {mod_folder_name}")

            # Then scan regular enabled MO2 mods
            if enabled_mo2_mods:
                for mo2_mod_name in enabled_mo2_mods:
                    mod_path = mo2_mods_path / mo2_mod_name
                    mods_subdir = mod_path / "Mods"

                    if mods_subdir.exists() and mods_subdir.is_dir():
                        for subdir in mods_subdir.iterdir():
                            if subdir.is_dir() and not subdir.name.startswith('.'):
                                mod_folder_name = subdir.name
                                if mod_folder_name not in witcher3_mods:
                                    witcher3_mods.append(mod_folder_name)
                                    mod_folder_to_mo2_mod[mod_folder_name] = mo2_mod_name

            # Show warning message if Overwrite mods are found
            if overwrite_mods:
                overwrite_msg = f"⚠️ Found {len(overwrite_mods)} Witcher 3 mod(s) in Overwrite folder.\n"
                overwrite_msg += "Consider creating proper mods from Overwrite contents for better management.\n"
                overwrite_msg += f"Overwrite mods: {', '.join(overwrite_mods[:3])}"
                if len(overwrite_mods) > 3:
                    overwrite_msg += f" (and {len(overwrite_mods) - 3} more)"
                self.status_label.setText(overwrite_msg)
                self.status_label.setStyleSheet("color: orange; font-weight: bold; margin: 5px;")
                logger.warning(f"Overwrite mods detected: {overwrite_mods}")
            else:
                self.status_label.setText("")

            if not witcher3_mods:
                self.mod_list.clear()
                # Clear in-memory entries to match the empty UI
                self.settings_manager.mod_entries = []
                if self.sync_enabled:
                    self.settings_manager.save_mods_settings()
                    logger.info("No Witcher 3 mods found, cleared mods.settings")
                else:
                    logger.info("No Witcher 3 mods found, skipped saving mods.settings (sync disabled)")
                return

            # Update UI
            self.mod_list.set_mod_mapping(mod_folder_to_mo2_mod)
            self.mod_list.clear()

            # Load existing order from settings
            self.settings_manager.load_mods_settings()
            existing_entries = {entry.name: entry for entry in self.settings_manager.mod_entries}

            # Create ordered list with Overwrite mods at the top (highest priority)
            ordered_mods = []

            # First add Overwrite mods at the top
            for mod_name in overwrite_mods:
                ordered_mods.append(mod_name)
                witcher3_mods.remove(mod_name)

            # Then add existing order from settings
            for entry in self.settings_manager.mod_entries:
                if entry.name in witcher3_mods:
                    ordered_mods.append(entry.name)
                    witcher3_mods.remove(entry.name)

            # Finally add any new mods not in settings
            ordered_mods.extend(witcher3_mods)

            # Add to UI with enabled state
            for mod_name in ordered_mods:
                # Check if mod has existing enabled state, default to True for new mods
                enabled = existing_entries.get(mod_name, ModEntry(mod_name, True, 0)).enabled

                # Special tooltip for Overwrite mods
                tooltip = None
                if mod_name in overwrite_mods:
                    tooltip = "⚠️ This mod is in the Overwrite folder (highest priority)\nConsider creating a proper mod from Overwrite contents"

                self.mod_list.add_mod_item(mod_name, enabled, tooltip)

            self.mod_list.renumber()

            # Update settings
            self.settings_manager.sync_with_mod_list(ordered_mods)
            if self.sync_enabled:
                self.settings_manager.save_mods_settings()
                logger.debug("Saved mods.settings during refresh (sync enabled)")
            else:
                logger.debug("Skipped saving mods.settings during refresh (sync disabled)")

            logger.info(f"Loaded {len(ordered_mods)} Witcher 3 mods ({len(overwrite_mods)} from Overwrite)")

        except Exception as e:
            logger.error(f"Error refreshing mod list: {e}")

    def sync_to_settings(self):
        """Sync current UI order and enabled states to mods.settings"""
        try:
            # Check if profile changed before syncing
            self.check_and_update_profile()

            if not self.settings_manager:
                logger.warning("No settings manager available for sync")
                return

            mods_with_state = self.mod_list.get_mods_with_enabled_state()
            mod_entries = []

            for i, (mod_name, enabled) in enumerate(mods_with_state):
                mod_entries.append(ModEntry(mod_name, enabled, i))

            self.settings_manager.mod_entries = mod_entries

            if self.settings_manager.save_mods_settings():
                enabled_count = sum(1 for _, enabled in mods_with_state if enabled)
                logger.info(f"Synced {len(mod_entries)} mods to mods.settings ({enabled_count} enabled)")
            else:
                logger.error("Failed to save mods.settings")

        except Exception as e:
            logger.error(f"Error syncing to settings: {e}")

    def get_enabled_mods(self) -> List[str]:
        """Get list of enabled mods from MO2 profile"""
        try:
            modlist_path = Path(self.organizer.profilePath()) / "modlist.txt"
            enabled_mods = []

            if modlist_path.exists():
                with modlist_path.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("+"):
                            mod_name = line[1:]  # Remove '+' prefix
                            if not mod_name.endswith("_separator"):
                                enabled_mods.append(mod_name)
            else:
                logger.warning(f"modlist.txt not found at: {modlist_path}")

            return enabled_mods

        except Exception as e:
            logger.error(f"Error reading modlist.txt: {e}")
            return []

# ============================================================================
# Game Edition Detection
# ============================================================================

class Witcher3Edition(Enum):
    """Which build of the game is installed at the game path.

    CLASSIC     1.3x:  bin/x64 only (DX11)
    NEXT_GEN    4.0x:  bin/x64 (DX11) + bin/x64_dx12 (DX12)
    REMASTERED  5.x:   bin/x64_dx12 only; launcher-configuration.json
                       lists the "remasteredEdition" edition
    """

    CLASSIC = "Classic"
    NEXT_GEN = "Next-Gen"
    REMASTERED = "Remastered"
    UNKNOWN = "Unknown"


BINARY_DX11 = "bin/x64/witcher3.exe"
BINARY_DX12 = "bin/x64_dx12/witcher3.exe"
LAUNCHER_CONFIG = "launcher-configuration.json"
REMASTERED_EDITION_NAME = "remasteredEdition"


def _read_launcher_editions(game_dir: Path) -> set:
    """Edition names listed in the game's launcher-configuration.json."""
    try:
        config = json.loads((game_dir / LAUNCHER_CONFIG).read_text(encoding="utf-8"))
        return {
            e.get("name", "")
            for e in config.get("editions", [])
            if isinstance(e, dict)
        }
    except (OSError, ValueError, AttributeError, TypeError):
        return set()


def _file_major_version(path: Path) -> Optional[int]:
    """Major component of an executable's file version, if readable."""
    try:
        return int(mobase.getFileVersion(str(path)).split(".")[0])
    except Exception:
        return None


def detect_edition(game_dir: Path) -> Witcher3Edition:
    """Identify the installed edition from files in the game directory.

    Signals, most reliable first: the launcher config's edition list,
    then which bin folders exist, then the DX12 exe's major version
    (5.x = remaster, 4.x = next-gen) to tell the two DX12 builds apart
    when the launcher config is missing.
    """
    if REMASTERED_EDITION_NAME in _read_launcher_editions(game_dir):
        return Witcher3Edition.REMASTERED

    has_dx11 = (game_dir / BINARY_DX11).is_file()
    has_dx12 = (game_dir / BINARY_DX12).is_file()

    if not has_dx12:
        return Witcher3Edition.CLASSIC if has_dx11 else Witcher3Edition.UNKNOWN

    major = _file_major_version(game_dir / BINARY_DX12)
    if major is not None and major >= 5:
        return Witcher3Edition.REMASTERED
    if major == 4 or has_dx11:
        return Witcher3Edition.NEXT_GEN
    # DX12-only install with an unreadable version: only the remaster ships that way
    return Witcher3Edition.REMASTERED

# ============================================================================
# Main Game Plugin
# ============================================================================

class Witcher3SaveGame(BasicGameSaveGame):
    def allFiles(self):
        return [self._filepath.name, self._filepath.name.replace(".sav", ".png")]

class Witcher3Game(BasicGame):
    Name = "Witcher 3 Support Plugin"
    Author = "Evannee45"
    Version = "4.0.2"

    GameName = "The Witcher 3: Wild Hunt"
    GameShortName = "witcher3"
    GameNexusName = "witcher3"
    GameNexusId = 952
    GameSteamId = [499450, 292030]
    GameGogId = [1640424747, 1495134320, 1207664663, 1207664643]
    # Fallback only -- binaryName() picks the exe for the detected edition
    GameBinary = BINARY_DX11
    GameDataPath = ""
    GameSaveExtension = "sav"
    GameDocumentsDirectory = "%DOCUMENTS%/The Witcher 3"
    GameSavesDirectory = "%GAME_DOCUMENTS%/gamesaves"
    GameSupportURL = (
        r"https://github.com/ModOrganizer2/modorganizer-basic_games/wiki/"
        "Game:-The-Witcher-3"
    )

    def __init__(self):
        super().__init__()
        self.organizer = None
        self.mod_sorting_widget = None
        self._edition = Witcher3Edition.UNKNOWN

    # ------------------------------------------------------------------
    # Edition-aware game detection
    # ------------------------------------------------------------------

    def setGamePath(self, path) -> None:
        super().setGamePath(path)
        self._edition = detect_edition(Path(self._gamePath)) if self._gamePath else Witcher3Edition.UNKNOWN
        logger.info(f"Detected Witcher 3 edition: {self._edition.value} ({self._gamePath})")

    def edition(self) -> Witcher3Edition:
        return self._edition

    def _preferred_binaries(self) -> List[str]:
        """Candidate exes, best first. Next-gen keeps DX11 as the default
        executable, matching this plugin's behaviour before editions."""
        if self._edition == Witcher3Edition.REMASTERED:
            return [BINARY_DX12, BINARY_DX11]
        return [BINARY_DX11, BINARY_DX12]

    def binaryName(self) -> str:
        if self._gamePath:
            game_dir = Path(self._gamePath)
            for binary in self._preferred_binaries():
                if (game_dir / binary).is_file():
                    return binary
        return self._preferred_binaries()[0]

    def looksValid(self, directory: QDir) -> bool:
        return directory.exists(BINARY_DX11) or directory.exists(BINARY_DX12)

    def executables(self) -> List[mobase.ExecutableInfo]:
        """Default exe titled with the game name (keeps existing MO2 entries
        matching); the other renderer, if installed, gets its own entry."""
        game_dir = self.gameDirectory()
        primary = self.binaryName()
        execs = [
            mobase.ExecutableInfo(
                self.gameName(), QFileInfo(game_dir.absoluteFilePath(primary))
            )
        ]
        for binary, label in ((BINARY_DX11, "DirectX 11"), (BINARY_DX12, "DirectX 12")):
            if binary != primary and game_dir.exists(binary):
                execs.append(
                    mobase.ExecutableInfo(
                        f"{self.gameName()} ({label})",
                        QFileInfo(game_dir.absoluteFilePath(binary)),
                    )
                )
        return execs

    def init(self, organizer: mobase.IOrganizer):
        super().init(organizer)
        self.organizer = organizer

        # Register save game feature
        self._register_feature(BasicGameSaveGameInfo(lambda s: s.with_suffix(".png")))

        # Register UI initialization callback for mod sorting
        try:
            organizer.onUserInterfaceInitialized(self.init_mod_sorting_ui)
            logger.info("Registered mod sorting UI initialization callback")
        except Exception as e:
            logger.error(f"Failed to register UI callback: {e}")

        return True

    def init_mod_sorting_ui(self, window):
        """Initialize mod sorting UI when MO2 interface is ready"""
        try:
            if self.organizer.managedGame() != self:
                return  # Only initialize for Witcher 3

            logger.info("Initializing mod sorting UI")

            # Find the main tab widget
            tab_widget = window.findChild(QTabWidget, "tabWidget")
            if not tab_widget:
                logger.warning("No QTabWidget named 'tabWidget' found")
                return

            # Create and add the mod sorting widget as a new tab
            self.mod_sorting_widget = ModSortingWidget(self.organizer, window)

            # Find the position to insert between Data and Saves
            insert_index = -1
            for i in range(tab_widget.count()):
                tab_text = tab_widget.tabText(i)
                if "Saves" in tab_text or "Downloads" in tab_text:
                    insert_index = i
                    break

            if insert_index >= 0:
                tab_widget.insertTab(insert_index, self.mod_sorting_widget, "Mods")
                logger.info(f"Inserted Mods tab at position {insert_index}")
            else:
                tab_widget.addTab(self.mod_sorting_widget, "Mods")
                logger.info("Added Mods tab at the end (fallback)")

            logger.info("Mod sorting tab added successfully")

        except Exception as e:
            logger.error(f"Failed to initialize mod sorting UI: {e}")
            import traceback
            logger.error(traceback.format_exc())

    def iniFiles(self):
        """Return list of ini files that should be displayed in the Data tab"""
        files = ["user.settings", "input.settings", "mods.settings"]
        if self._edition != Witcher3Edition.CLASSIC:
            # The DX12 renderer (next-gen / remaster) keeps its own settings file
            files.insert(1, "dx12user.settings")
        return files

    def listSaves(self, folder) -> List[mobase.ISaveGame]:
        """Return list of save games in the given folder"""
        ext = self._mappings.savegameExtension.get()
        return [
            Witcher3SaveGame(path)
            for path in Path(folder.absolutePath()).glob(f"*.{ext}")
        ]
