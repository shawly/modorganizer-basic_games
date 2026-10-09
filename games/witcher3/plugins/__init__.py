import mobase

from ...game_witcher3 import Witcher3NestedInstaller


def createPlugins() -> list[mobase.IPluginInstallerSimple]:
    return [Witcher3NestedInstaller()]
