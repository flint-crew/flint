"""Share the reordered visibilities wsclean saves between wsclean calls"""

from __future__ import annotations

from pathlib import Path

from flint.logging import logger


def link_reordered_files(seed_dir: Path, target_dir: Path, ms_path: Path) -> Path:
    """Give ``target_dir`` the reordered files of ``ms_path`` saved in ``seed_dir``.

    The data, weights and metadata are hard linked. wsclean writes its model
    beside them, so each directory gets an empty model file of its own.
    """
    reordered_files = sorted(seed_dir.glob(f"{ms_path.name}-*.tmp"))
    assert reordered_files, f"No reordered files of {ms_path.name} in {seed_dir}"

    target_dir.mkdir(parents=True, exist_ok=True)
    for reordered_file in reordered_files:
        target = target_dir / reordered_file.name
        target.unlink(missing_ok=True)
        if reordered_file.name.endswith("-m.tmp"):
            with target.open("wb") as model_file:
                model_file.truncate(reordered_file.stat().st_size)
        else:
            target.hardlink_to(reordered_file)

    logger.info(f"Linked {len(reordered_files)} reordered files into {target_dir}")
    return target_dir
