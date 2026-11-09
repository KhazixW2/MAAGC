"""项目日志出口。

除了常规的分级日志，还提供一条「终端单行刷新」的进度通道：
连续刷新的进度信息（战斗回合进度等）在控制台原地覆盖同一行，
既不刷屏也不丢信息；日志文件里仍然逐条保留，方便事后回溯。
"""

import os
import sys
import threading

# 进度记录在 record["extra"] 里的标记位：控制台 sink 据此跳过逐条打印。
_PROGRESS_FLAG = "maagc_progress"

# 简短的日志等级名，替代 loguru 默认的长名字。
_LEVEL_SHORT = {
    "INFO": "info",
    "ERROR": "err",
    "WARNING": "warn",
    "DEBUG": "debug",
    "CRITICAL": "critical",
    "SUCCESS": "success",
    "TRACE": "trace",
}


def _format_level(record: dict) -> bool:
    """给记录补上简短的等级名，然后放行。"""
    record["extra"]["level_short"] = _LEVEL_SHORT.get(
        record["level"].name, record["level"].name.lower()
    )
    return True


def _console_filter(record: dict) -> bool:
    """控制台只打印常规日志。

    进度消息由 ProgressPrinter 在同一行原地刷新；如果这里放行，
    控制台会出现两条并排的进度文本。
    """
    if record["extra"].get(_PROGRESS_FLAG):
        return False
    return _format_level(record)


class ProgressPrinter:
    """终端单行刷新的进度输出。

    控制台用 ``\\r`` 回到行首重写同一行，并按历史最大宽度补空格，
    保证内容变短时不会留下残影；``record`` 回调负责把同样的文本
    逐条写进日志文件。
    """

    PREFIX = "进度: "

    def __init__(self, stream=None, record=None) -> None:
        self._stream = sys.stderr if stream is None else stream
        self._record = record
        self._width = 0
        self._active = False
        # 可重入锁：写日志文件的过程中可能反过来触发控制台收尾。
        self._lock = threading.RLock()

    def update(self, message: str, depth: int = 2) -> None:
        """刷新进度行：控制台原地覆盖，同时往日志文件追加一条记录。

        ``depth`` 是日志里的调用栈深度，用来把记录位置指向真正的业务代码；
        经过额外一层封装时调大它。
        """
        text = self.PREFIX + message
        with self._lock:
            if self._record is not None:
                self._record(text, depth)
            padding = " " * max(0, self._width - len(text))
            self._width = max(self._width, len(text))
            self._stream.write("\r" + text + padding)
            self._stream.flush()
            self._active = True

    def commit(self) -> None:
        """结束当前进度行：保留内容并换行，让后续日志另起一行。"""
        with self._lock:
            if not self._active:
                return
            self._stream.write("\n")
            self._stream.flush()
            self._active = False
            self._width = 0


try:
    from loguru import logger as _logger

    def setup_logger(log_dir=None, console_level=None):
        """设置 loguru logger

        Args:
            log_dir: 日志文件目录
            console_level: 控制台输出等级 (DEBUG, INFO, WARNING, ERROR)
        """
        log_dir = log_dir or os.environ.get("MAAGC_DEBUG_LOG_DIR", "debug/custom")
        console_level = console_level or os.environ.get(
            "MAAGC_CONSOLE_LOG_LEVEL", "INFO"
        )
        os.makedirs(log_dir, exist_ok=True)
        _logger.remove()

        # 定义日志级别的简短格式
        def console_format(record):
            """打印常规日志前先收尾进度行，避免两条日志粘在同一行。

            这里用可调用格式而不是静态字符串，是因为只有真正要打印到
            控制台时才该收尾进度行（写日志文件的记录不该影响终端）。
            """
            progress.commit()
            return (
                "<level>{extra[level_short]}</level>:<level>{message}</level>"
                "\n{exception}"
            )

        _logger.add(
            sys.stderr,
            format=console_format,
            colorize=True,
            level=console_level,
            filter=_console_filter,
        )
        _logger.add(
            f"{log_dir}/{{time:YYYY-MM-DD}}.log",
            rotation="00:00",  # midnight
            retention="2 weeks",
            compression="zip",
            level="DEBUG",
            format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} | {message}",
            encoding="utf-8",
            enqueue=True,
            backtrace=True,  # 包含完整的异常回溯信息
            diagnose=False,
        )
        return _logger

    def change_console_level(level="INFO"):
        """动态修改控制台日志等级"""
        setup_logger(console_level=level)
        _logger.info(f"控制台日志等级已更更为: {level}")

    def _record_progress(text: str, depth: int = 2) -> None:
        """把进度文本逐条写进日志文件。

        控制台由 ProgressPrinter 自己原地刷新，这里带标记位让控制台
        sink 跳过这条记录，避免同一段进度在终端上出现两次。
        ``depth`` 让日志里的调用位置指向真正的业务代码，而不是这里。
        """
        _logger.bind(**{_PROGRESS_FLAG: True}).opt(depth=depth).info(text)

    progress = ProgressPrinter(record=_record_progress)
    logger = setup_logger()
except ImportError:
    import logging

    class ShortLevelFormatter(logging.Formatter):
        """自定义 Formatter，使用简短的日志级别名称"""

        level_map = {
            "INFO": "info",
            "ERROR": "err",
            "WARNING": "warn",
            "DEBUG": "debug",
            "CRITICAL": "critical",
        }

        def format(self, record):
            record.level_short = self.level_map.get(
                record.levelname, record.levelname.lower()
            )
            return super().format(record)

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(ShortLevelFormatter("%(level_short)s:%(message)s"))
    logging.root.addHandler(handler)
    logging.root.setLevel(logging.INFO)
    logger = logging
    # 没有 loguru 时没有文件 sink，进度只出现在终端。
    progress = ProgressPrinter()
