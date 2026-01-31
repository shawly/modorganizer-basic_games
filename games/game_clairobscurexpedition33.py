import mobase

from ..basic_features import BasicModDataChecker, GlobPatterns
from ..basic_game import BasicGame


class ClairObscurExpedition33ModDataChecker(BasicModDataChecker):
    def __init__(self):
        super().__init__(
            GlobPatterns(
                move={
                    "*.pak": "Sandfall/Content/Paks/~mods/",
                    "*.ucas": "Sandfall/Content/Paks/~mods/",
                    "*.utoc": "Sandfall/Content/Paks/~mods/",
                    "*.asi": "Sandfall/Binaries/Win64/",
                    "*.dll": "Sandfall/Binaries/Win64/",
                    "*.ini": "Sandfall/Binaries/Win64/",
                    "Content": "Sandfall/",
                    "Paks": "Sandfall/Content/",
                },
                valid=["Sandfall"],
            )
        )


class ClairObscurExpedition33Game(BasicGame):
    Name = "Clair Obscur: Expedition 33 Support Plugin"
    Author = "shawly"
    Version = "0.0.1"

    GameName = "Clair Obscur: Expedition 33"
    GameShortName = "clairobscurexpedition33"
    GameNexusName = "clairobscurexpedition33"
    GameNexusId = 7635
    GameSteamId = 1903340
    GameGogId = 2125022825
    GameBinary = "Sandfall/Binaries/Win64/SandFall-Win64-Shipping.exe"
    GameDataPath = "%GAME_PATH%"
    GameDocumentsDirectory = "%USERPROFILE%/AppData/Local/Sandfall"
    GameIniFiles = [
        "%GAME_DOCUMENTS%/Saved/Config/Windows/Game.ini",
        "%GAME_DOCUMENTS%/Saved/Config/Windows/GameUserSettings.ini",
    ]
    GameSavesDirectory = "%GAME_DOCUMENTS%/Saved/SaveGames"
    GameSaveExtension = "sav"

    def init(self, organizer: mobase.IOrganizer) -> bool:
        super().init(organizer)
        self._register_feature(ClairObscurExpedition33ModDataChecker())
        return True
