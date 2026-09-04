from importlib.metadata import version

import music_friend


def test_package_exposes_installed_version() -> None:
    assert music_friend.__version__ == version("music-friend")
