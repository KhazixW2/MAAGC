"""统一的截图读取。

原先 fight_utils / pirate_raid_processor / auto_fight_processor 各有一份
逐字相同的 ``_screencap``，截图失败语义靠约定维持一致。这里收敛成一份实现：
截图失败一律返回 None，由调用方决定如何处理，不抛异常。
"""

from typing import Any, Optional

from maa.context import Context


def screencap(context: Context) -> Optional[Any]:
    """截取当前画面；失败返回 None。"""
    job = context.tasker.controller.post_screencap().wait()
    if not job.succeeded:
        return None
    return job.get()
