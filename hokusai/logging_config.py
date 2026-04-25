"""
Logging Configuration

ワークフローのログ設定を管理するモジュール。
"""

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# ログフォーマット
VERBOSE_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
SIMPLE_FORMAT = "%(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(
    verbose: bool = False,
    log_file: Optional[Path] = None,
    log_level: int = logging.INFO,
) -> logging.Logger:
    """
    ロギングを設定

    Args:
        verbose: 詳細ログを有効にするか
        log_file: ログファイルのパス（省略時はコンソールのみ）
        log_level: ログレベル（デフォルト: INFO）

    Returns:
        設定済みのルートロガー
    """
    # ルートロガーを取得
    root_logger = logging.getLogger("hokusai")
    root_logger.setLevel(logging.DEBUG if verbose else log_level)

    # 既存のハンドラをクリア
    root_logger.handlers.clear()

    # フォーマッタを作成
    if verbose:
        formatter = logging.Formatter(VERBOSE_FORMAT, DATE_FORMAT)
    else:
        formatter = logging.Formatter(SIMPLE_FORMAT)

    # コンソールハンドラ
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG if verbose else log_level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # ファイルハンドラ（指定された場合）
    if log_file:
        log_file = Path(log_file)
        log_file.parent.mkdir(parents=True, exist_ok=True)

        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)  # ファイルには常に詳細ログ
        file_formatter = logging.Formatter(VERBOSE_FORMAT, DATE_FORMAT)
        file_handler.setFormatter(file_formatter)
        root_logger.addHandler(file_handler)

        root_logger.info(f"ログファイル: {log_file}")

    return root_logger


def get_logger(name: str) -> logging.Logger:
    """
    名前付きロガーを取得

    Args:
        name: ロガー名（通常はモジュール名）

    Returns:
        ロガー
    """
    return logging.getLogger(f"hokusai.{name}")


def get_default_log_path() -> Path:
    """
    デフォルトのログファイルパスを取得

    Returns:
        ログファイルのパス（~/.hokusai/logs/workflow_YYYYMMDD_HHMMSS.log）
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = Path.home() / ".hokusai" / "logs"
    return log_dir / f"workflow_{timestamp}.log"
