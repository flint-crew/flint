"""Join the clean component lists wsclean saves for each island"""

from __future__ import annotations

import re
from pathlib import Path

from flint.logging import logger


def concatenate_source_lists(
    source_lists: list[Path], tags: list[str], output: Path
) -> Path | None:
    """Join wsclean source lists into one, or return None if none hold a component.

    Each component name is prefixed by the tag of its list, as wsclean names the
    components of every list alike. The reference frequency of a list follows its
    imaging weights, so it may differ between lists. Each component carries its
    own, and the one in the header is only a default.
    """
    assert len(source_lists) == len(tags), "A tag is needed for each source list"

    header = None
    components = []
    for source_list, tag in zip(source_lists, tags, strict=True):
        if not source_list.exists():
            continue
        lines = [line for line in source_list.read_text().splitlines() if line.strip()]
        if len(lines) < 2:
            continue
        columns = re.sub(r"ReferenceFrequency='[^']*'", "ReferenceFrequency", lines[0])
        if header is None:
            header, header_columns = lines[0], columns
        elif columns != header_columns:
            msg = f"The columns of {source_list} differ from the earlier source lists"
            raise ValueError(msg)
        components.extend(f"{tag}{line}" for line in lines[1:])

    if header is None:
        logger.warning("No components found in the source lists")
        return None

    logger.info(f"Writing {len(components)} components to {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join([header, *components]) + "\n")
    return output
