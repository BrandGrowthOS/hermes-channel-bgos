from enum import Enum

from ._results import *  # noqa: F401,F403 - SendResult is re-exported from here


class BasePlatformAdapter:
    pass


class MessageEvent:
    pass


class MessageType(Enum):
    TEXT = "text"


def build_session_key(platform, chat_id):
    return f"{platform}:{chat_id}"


if True:  # a name bound inside a module level block still counts
    def cache_audio_from_bytes(data, ext=".ogg"):
        return ""


annotated_only: int  # an annotation without a value binds nothing
