"""
Unpack an Office file into a new directory.

Usage:
    python unpack.py <file> <new_dir>

<new_dir> must not exist yet; it is created by this command. Symlink entries
and entries that would land outside <new_dir> are refused.
"""

import argparse
import sys
import zipfile
from pathlib import Path

from helpers import extract_to_new_dir


def main():
    parser = argparse.ArgumentParser(description="Unpack an Office file into a new directory")
    parser.add_argument("file", help="Packed Office file (.docx/.pptx/.xlsx or .dotx/.potx/.xltx)")
    parser.add_argument("new_dir", help="Directory to create and unpack into; must not exist")
    args = parser.parse_args()

    try:
        with zipfile.ZipFile(args.file) as zf:
            extract_to_new_dir(zf, Path(args.new_dir))
    except (OSError, ValueError, zipfile.BadZipFile) as e:
        print(f"Error: cannot unpack {args.file}: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
