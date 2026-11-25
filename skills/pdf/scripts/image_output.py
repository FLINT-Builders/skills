import os
import secrets
from pathlib import Path

from PIL import Image


def save_image(image, path):
    # The image goes to a fresh file and is renamed over `path`; the rename
    # replaces whatever entry is there, so a symlink is never followed.
    path = Path(path)
    image_format = Image.registered_extensions().get(path.suffix.lower())
    if image_format is None:
        raise ValueError(f"unknown file extension: {path.suffix!r}")
    if path.is_symlink():
        raise ValueError(f"Refusing to write through symbolic link: {path}")

    dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        tmp_name = f".{path.name}.{secrets.token_hex(8)}.tmp"
        fd = os.open(
            tmp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o666,
            dir_fd=dir_fd,
        )
        try:
            with os.fdopen(fd, "wb") as fh:
                image.save(fh, image_format)
            os.replace(tmp_name, path.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except BaseException:
            try:
                os.unlink(tmp_name, dir_fd=dir_fd)
            except FileNotFoundError:
                pass
            raise
    finally:
        os.close(dir_fd)
