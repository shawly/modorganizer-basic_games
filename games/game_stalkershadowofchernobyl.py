from __future__ import annotations

from enum import IntEnum

import mobase
from PyQt6.QtCore import QDir, QFileInfo

from ..basic_features import BasicGameSaveGameInfo, BasicModDataChecker, GlobPatterns
from ..basic_game import BasicGame

# Fixes compatibility with BasicLocalSavegames feature from MO2 v2.5.2
class StalkerSoCLocalSavegames(mobase.LocalSavegames):
    def __init__(self, savesDir: QDir):
        super().__init__()
        self._savesDir = savesDir

    def mappings(self, profile_save_dir: QDir):
        return [
            mobase.Mapping(
                source=profile_save_dir.absolutePath(),
                destination=self._savesDir.absolutePath(),
                is_directory=True,
                create_target=True,
            )
        ]

    def prepareProfile(self, profile: mobase.IProfile) -> bool:
        return profile.localSavesEnabled()


class StalkerSoCModDataChecker(BasicModDataChecker):
    def __init__(self):
        super().__init__(
            GlobPatterns(
                valid=[
                    "gamedata",
                    "bin",
                    "bin_x64",
                    "users",
                    "appdata",
                    "mods",
                ],
                move={
                    "*.db*": "gamedata/",
                    "*.ltx": "gamedata/config/",
                    "*.xml": "gamedata/config/",
                    "textures": "gamedata/textures/",
                    "meshes": "gamedata/meshes/",
                    "sounds": "gamedata/sounds/",
                    "scripts": "gamedata/scripts/",
                    "config": "gamedata/config/",
                    "configs": "gamedata/config/",
                },
            )
        )


class Content(IntEnum):
    INTERFACE = 0
    TEXTURE = 1
    MESH = 2
    SCRIPT = 3
    SOUND = 4
    CONFIG = 5


class StalkerSoCModDataContent(mobase.ModDataContent):
    content: list[int] = []

    def getAllContents(self) -> list[mobase.ModDataContent.Content]:
        return [
            mobase.ModDataContent.Content(
                Content.INTERFACE, "Interface", ":/MO/gui/content/interface"
            ),
            mobase.ModDataContent.Content(
                Content.TEXTURE, "Textures", ":/MO/gui/content/texture"
            ),
            mobase.ModDataContent.Content(
                Content.MESH, "Meshes", ":/MO/gui/content/mesh"
            ),
            mobase.ModDataContent.Content(
                Content.SCRIPT, "Scripts", ":/MO/gui/content/script"
            ),
            mobase.ModDataContent.Content(
                Content.SOUND, "Sounds", ":/MO/gui/content/sound"
            ),
            mobase.ModDataContent.Content(
                Content.CONFIG, "Configs", ":/MO/gui/content/inifile"
            ),
        ]

    def walkContent(
        self, path: str, entry: mobase.FileTreeEntry
    ) -> mobase.IFileTree.WalkReturn:
        if entry.isFile():
            ext = entry.suffix().lower()
            if ext in ["dds", "thm", "tga"]:
                self.content.append(Content.TEXTURE)
                if path.startswith("gamedata/textures/ui"):
                    self.content.append(Content.INTERFACE)
            elif ext in ["omf", "ogf"]:
                self.content.append(Content.MESH)
            elif ext in ["script"]:
                self.content.append(Content.SCRIPT)
            elif ext in ["ogg", "wav"]:
                self.content.append(Content.SOUND)
            elif ext in ["ltx", "xml"]:
                self.content.append(Content.CONFIG)
                if path.startswith("gamedata/config/ui"):
                    self.content.append(Content.INTERFACE)

        return mobase.IFileTree.WalkReturn.CONTINUE

    def getContentsFor(self, filetree: mobase.IFileTree) -> list[int]:
        self.content = []
        filetree.walk(self.walkContent, "/")
        return self.content


class StalkerSoCGame(BasicGame, mobase.IPluginFileMapper):
    Name = "S.T.A.L.K.E.R.: SoC Support Plugin"
    Author = "shawly"
    Version = "1.0.0"

    GameName = "S.T.A.L.K.E.R.: Shadow of Chernobyl"
    GameShortName = "stalkershadowofchernobyl"
    GameSteamId = 4500
    GameNexusId = 1428
    GameBinary = "bin/XR_3DA.exe"
    GameDataPath = ""

    def __init__(self):
        BasicGame.__init__(self)
        mobase.IPluginFileMapper.__init__(self)

    def init(self, organizer: mobase.IOrganizer) -> bool:
        if not super().init(organizer):
            return False
        self._register_feature(StalkerSoCModDataChecker())
        self._register_feature(StalkerSoCModDataContent())
        self._register_feature(StalkerSoCLocalSavegames(self.savesDirectory()))
        self._register_feature(
            BasicGameSaveGameInfo(
                lambda s: s.with_suffix(".dds")
                if s.with_suffix(".dds").exists()
                else None
            )
        )
        organizer.onAboutToRun(lambda _str: self.aboutToRun(_str))
        return True

    def aboutToRun(self, _str: str) -> bool:
        gamedir = self.gameDirectory()
        if gamedir.exists():
            # For mappings
            gamedir.mkdir("_appdata_")
        return True

    def mappings(self) -> list[mobase.Mapping]:
        appdata = self.gameDirectory().filePath("_appdata_")
        m = mobase.Mapping(
            source=self.gameDirectory().filePath("_appdata_"),
            destination=self.gameDirectory().filePath("_appdata_"),
            is_directory=True,
            create_target=True,
        )
        return [m]
    
    def executables(self):
        return [
            mobase.ExecutableInfo(
                self.gameName(),
                QFileInfo(
                    self.gameDirectory(),
                    "bin/XR_3DA.exe",
                ),
            ),
            mobase.ExecutableInfo(
                "OGSR Engine",
                QFileInfo(
                    self.gameDirectory(),
                    "bin_x64/xrEngine.exe",
                ),
            ),
        ]
