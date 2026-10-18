"""MCP 调用日志；仅写 stderr，避免干扰 stdio 协议。"""
from functools import wraps
import inspect
import json
import logging
import time
from uuid import uuid4

logger = logging.getLogger('hscode.calls')


class SessionNoiseFilter(logging.Filter):
    def filter(self, record):
        return record.getMessage() != 'Terminating session: None'


def configure_logging(level: str) -> None:
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
        logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    protocol_logger = logging.getLogger('mcp.server.streamable_http')
    if not any(isinstance(f, SessionNoiseFilter) for f in protocol_logger.filters):
        protocol_logger.addFilter(SessionNoiseFilter())


def logged_call(kind: str):
    def decorate(function):
        signature = inspect.signature(function)

        @wraps(function)
        def wrapped(*args, **kwargs):
            call_id = uuid4().hex[:12]
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            params = {key: (value[:200] + '…' if isinstance(value, str) and len(value) > 200 else value)
                      for key, value in bound.arguments.items()}
            prefix = f'call_id={call_id} kind={kind} name={function.__name__}'
            started = time.perf_counter()
            logger.info('%s event=start params=%s', prefix, json.dumps(params, ensure_ascii=False))
            try:
                result = function(*args, **kwargs)
            except Exception as exc:
                # 不输出连接串、异常原文或返回正文；堆栈保留文件、行号和调用路径。
                import traceback
                frames = ''.join(traceback.format_tb(exc.__traceback__))
                logger.error('%s event=error elapsed_ms=%.1f error_type=%s\n%s',
                             prefix, (time.perf_counter() - started) * 1000, type(exc).__name__, frames)
                raise
            summary = {}
            if isinstance(result, dict):
                summary = {key: result[key] for key in ('found', 'match', 'count', 'total', 'anchor_hs6')
                           if key in result}
            logger.info('%s event=done elapsed_ms=%.1f result=%s', prefix,
                        (time.perf_counter() - started) * 1000,
                        json.dumps(summary, ensure_ascii=False))
            return result
        return wrapped
    return decorate
