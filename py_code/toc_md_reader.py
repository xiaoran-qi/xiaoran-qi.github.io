import contextlib
import copy
import datetime
import logging
import re

from pelican.contents import Author, Category, Tag
from pelican.plugins import signals
from pelican.readers import DUPLICATES_DEFINITIONS_ALLOWED, MarkdownReader
from pelican.utils import get_date, pelican_open
from pathlib import Path

# Only enable this extension if yaml and markdown packages are installed
ENABLED = False
with contextlib.suppress(ImportError):
    from markdown import Markdown
    import yaml

    ENABLED = True


__log__ = logging.getLogger(__name__)

HEADER_RE = re.compile(
    r"\s*^---$"  # File starts with a line of "---" (preceeding blank lines accepted)
    r"(?P<metadata>.+?)"
    r"^(?:---|\.\.\.)$"  # metadata section ends with a line of "---" or "..."
    r"(?P<content>.*)",
    re.MULTILINE | re.DOTALL,
)

DUPES_NOT_ALLOWED = set(
    k for k, v in DUPLICATES_DEFINITIONS_ALLOWED.items() if not v
) - {"tags", "authors"}

_DEL = object()

YAML_METADATA_PROCESSORS = {
    "tags": lambda x, y: [Tag(_strip(t), y) for t in _to_list(x)] or _DEL,
    "date": lambda x, y: _parse_date(x),
    "modified": lambda x, y: _parse_date(x),
    "category": lambda x, y: Category(_strip(x), y) if x else _DEL,
    "author": lambda x, y: Author(_strip(x), y) if x else _DEL,
    "authors": lambda x, y: [Author(_strip(a), y) for a in _to_list(x)] or _DEL,
    "slug": lambda x, y: _strip(x) or _DEL,
    "save_as": lambda x, y: _strip(x) or _DEL,
    "status": lambda x, y: _strip(x) or _DEL,
    "path_no_ext": lambda x, y: x.replace('pages/', '') 
}


def _strip(obj):
    return str(obj).strip()


def _to_list(obj):
    """Make object into a list."""
    return [obj] if not isinstance(obj, (tuple, list)) else obj


def _parse_date(obj):
    """Return a string representing a date."""
    # If it's already a date object, make it a string so Pelican can parse it
    # and make sure it has a timezone
    if isinstance(obj, datetime.date):
        obj = obj.isoformat()

    return get_date(str(obj).strip().replace("_", " "))


class TOCMarkdownReader(MarkdownReader):
    """Reader for Markdown files with YAML metadata."""

    enabled = ENABLED

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Don't use default Markdown metadata extension for parsing. Leave self.settings
        # alone in case we have to fall back to normal Markdown parsing.
        md_settings = copy.deepcopy(self.settings["MARKDOWN"])
        with contextlib.suppress(KeyError, ValueError):
            md_settings["extensions"].remove("markdown.extensions.meta")
        self._md = Markdown(**md_settings)

    def read(self, source_path):
        """Parse content and YAML metadata of Markdown files."""
        with pelican_open(source_path) as text:
            m = HEADER_RE.fullmatch(text)

        if not m:
            __log__.info(
                (
                    "No YAML metadata header found in '%s' - "
                    "falling back to Markdown metadata parsing."
                ),
                source_path,
            )
            content, metadata = super().read(source_path)
        else:
            content, metadata = (
                self._md.reset().convert(m.group("content")),
                self._load_yaml_metadata(m.group("metadata"), source_path),
            )

            if "toc" in metadata:
                toc_md = Markdown(extensions=['toc'])
                _ = toc_md.convert(m.group("content"))
                metadata["parsed_toc"] = toc_md.toc_tokens

        return content, metadata

    def _load_yaml_metadata(self, text, source_path):
        """Load Pelican metadata from the specified text.

        Returns an empty dict if the data fails to parse properly.
        """
        try:
            metadata = yaml.safe_load(text)
        except Exception:  # NOQA: BLE001, RUF100
            __log__.error(
                "Error parsing YAML for file '%s",
                source_path,
                exc_info=True,
            )
            return {}

        if not isinstance(metadata, dict):
            __log__.error(
                "YAML header didn't parse as a dict for file '%s'",
                source_path,
            )
            __log__.debug("YAML data: %r", metadata)
            return {}

        return self._parse_yaml_metadata(metadata, source_path)

    def process_metadata(self, name, value):
        if name in YAML_METADATA_PROCESSORS:
            return YAML_METADATA_PROCESSORS[name](value, self.settings)
        return value

    def _parse_yaml_metadata(self, meta, source_path):
        """Parse YAML-provided data into Pelican metadata.

        Based on MarkdownReader._parse_metadata.
        """
        output = {}
        for name, value in meta.items():
            if value is None:
                continue

            name = name.lower()
            is_list = isinstance(value, list)
            if is_list:
                value = [x for x in value if x is not None]

            if name in self.settings["FORMATTED_FIELDS"]:
                # join mutliple formatted fields before parsing them as markdown
                value = self._md.reset().convert(
                    "\n".join(value) if is_list else str(value)
                )
            elif is_list and len(value) > 1 and name == "author":
                # special case: upconvert multiple "author" values to "authors"
                name = "authors"
            elif is_list and name in DUPES_NOT_ALLOWED:
                if len(value) > 1:
                    __log__.warning(
                        (
                            "Duplicate definition of '%s' for '%s' ('%r') - "
                            "using the first one ('%s')"
                        ),
                        name,
                        source_path,
                        value,
                        value[0],
                    )
                value = value[0]

            # Need to do our own metadata processing as YAML loads data in a
            # different way than the markdown metadata extension.
            if name in YAML_METADATA_PROCESSORS:
                value = YAML_METADATA_PROCESSORS[name](value, self.settings)
            if value is not _DEL:
                output[name] = value

        __log__.debug("Parsed YAML data: %r into Pelican data %r", meta, output)
        return output
