"""Keep WebSocket data payloads out of protocol logs."""

from __future__ import annotations

import logging

from websockets.frames import OP_BINARY, OP_CONT, OP_TEXT, Frame
from websockets.legacy.framing import Frame as LegacyFrame


class _WithoutDataFrames(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Inspect before formatting. Clipboard text can be in JSON, pasted bytes,
        # or a continuation frame carrying only part of the payload.
        return not any(
            isinstance(arg, (Frame, LegacyFrame)) and arg.opcode in (OP_TEXT, OP_BINARY, OP_CONT)
            for arg in (record.args if isinstance(record.args, tuple) else ())
        )


_without_data_frames = _WithoutDataFrames()


def websocket_protocol_logger(name: str) -> logging.Logger:
    """Retain connection diagnostics while dropping data-frame records."""
    logger = logging.getLogger(name)
    logger.addFilter(_without_data_frames)
    return logger
