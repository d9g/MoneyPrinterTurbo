"""
audio.id3_utils — ID3 tag 读取
v2-1 重构 (v2-7 22:18 升级为 ID3Metadata dataclass)

从 mp3 文件读取 artist/title/album/year 用于歌曲识别 (优先级 1)
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class ID3Metadata:
    """ID3 tag 元数据 (歌曲指纹第一层)"""
    artist: Optional[str] = None
    title: Optional[str] = None
    album: Optional[str] = None
    year: Optional[str] = None

    @property
    def is_empty(self) -> bool:
        """所有字段都为空"""
        return not any([self.artist, self.title, self.album, self.year])

    def to_dict(self) -> dict:
        return {
            "artist": self.artist,
            "title": self.title,
            "album": self.album,
            "year": self.year,
        }


def read_id3_tags(path: str) -> ID3Metadata:
    """读取 mp3 的 ID3 tag (artist/title/album/year)

    Returns: ID3Metadata — 任意字段缺失为 None, 完全失败返回空 ID3Metadata
    """
    try:
        from mutagen.mp3 import MP3
        from mutagen.id3 import ID3NoHeaderError

        audio = MP3(path, ID3=ID3NoHeaderError)
        tags = audio.tags

        if tags is None:
            return ID3Metadata()

        artist = _first_tag(tags, ["TPE1", "TPE2", "TPE3", "artist"])
        title = _first_tag(tags, ["TIT2", "TIT1", "title"])
        album = _first_tag(tags, ["TALB", "album"])
        year = _first_tag(tags, ["TDRC", "TYER", "year"])

        return ID3Metadata(artist=artist, title=title, album=album, year=year)

    except Exception:
        # ID3 缺失/损坏都返回空 ID3Metadata, 不影响主流程
        return ID3Metadata()


# 旧 API 兼容 (返回 tuple)
def read_id3_tags_legacy(path: str) -> tuple:
    """旧 API 兼容: 返回 (artist, title) tuple

    用于直接调用 read_id3_tags() 的代码
    """
    meta = read_id3_tags(path)
    return meta.artist, meta.title


# 别名: extract_id3_metadata (v2-7 controller 用)
def extract_id3_metadata(path: str) -> ID3Metadata:
    """read_id3_tags 的别名"""
    return read_id3_tags(path)


def _first_tag(tags, keys: list) -> Optional[str]:
    """从多个 tag key 中取第一个非空值"""
    for key in keys:
        try:
            value = tags.get(key)
            if value and value.text:
                text = str(value.text[0]).strip()
                if text:
                    return text
        except (AttributeError, IndexError):
            continue
    return None