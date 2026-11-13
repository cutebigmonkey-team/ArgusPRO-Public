"""Optional private config files must live outside the public application tree."""
import os
from pathlib import Path
import re


def private_asset_path(name):
    if not re.fullmatch(r'[a-z0-9-]+\.json',name):raise ValueError('invalid_private_asset_name')
    root=os.environ.get('ARGUS_PRIVATE_ASSETS_DIR')
    if not root:return Path(__file__).with_name(name)
    directory=Path(root)
    project=Path(__file__).resolve().parents[2]
    if not directory.is_absolute() or directory.resolve().is_relative_to(project):
        raise ValueError('private_assets_must_be_outside_source_tree')
    path=(directory/name).resolve()
    if not path.is_relative_to(directory.resolve()) or not path.is_file():
        raise ValueError('private_asset_missing')
    return path
