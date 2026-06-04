import argparse
import sys
import logging

# Reconfigure stdout to use UTF-8 on Windows console
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from screener.config import Config
from screener.data_fetcher import DataFetcher
from screener.strategy import StrategyEvaluator
from screener.notifier import LineNotifier
from screener import storage

# ── ロギング設定 ────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
def parse_args():
    parser = argparse.ArgumentParser(
        description="STELLAR SCREENER — 日本株スイングトレード自動スクリーナー"
    )
    parser.add_argument(
        "--config",
        type=str,
        default="config.yaml",
        help="YAML 設定ファイルのパス（デフォルト: config.yaml）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="スクリーニング結果をコンソールに出力するだけ（LINE 通知を送らない）",
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help="Web ダッシュボードサーバーを port 8000 で起動する",
    )
    parser.add_argument(
        "--jpx400",
        action="store_true",
        help="JPX400 全銘柄をスキャンする（config.yaml の tickers より優先）",
    )
    return parser.parse_args()


# ─────────────────────────────────────────────────────────────────────────────
def run_screening(config: Config, tickers: list, scan_type: str = "manual") -> list:
    """
    指定 tickers を一括スクリーニングして、BUY SIGNAL リストを返す。
    スキャン結果は SQLite に保存される。
    """
    import uuid
    from datetime import datetime

    scan_id = f"{scan_type}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    logger.info(f"スキャン開始: scan_id={scan_id}  対象={len(tickers)} 銘柄")

    storage.init_db()
    storage.create_session(scan_id=scan_id, scan_type=scan_type, total_tickers=len(tickers))

    fetcher   = DataFetcher(
        delay_seconds=config.delay_seconds,
        history_period=config.history_period,
    )
    evaluator = StrategyEvaluator(config.data)

    all_results  = []
    buy_signals  = []

    for i, ticker in enumerate(tickers):
        try:
            df, name = fetcher.fetch_ticker_data(ticker)
            if df is None or df.empty:
                logger.warning(f"[{i+1}/{len(tickers)}] {ticker}: データなし — スキップ")
                continue

            ev = evaluator.evaluate(ticker, name or ticker, df)

            # float 変換（SQLite 安全化）
            for k in ("current_price", "change_percent", "rsi",
                      "ma5", "ma25", "ma75", "bb_upper", "bb_lower",
                      "ma25_deviation_pct", "macd", "macd_signal", "macd_hist"):
                if ev.get(k) is not None:
                    ev[k] = float(ev[k])

            storage.save_result(scan_id, ev)
            all_results.append(ev)

            if ev["buy_signal"]:
                buy_signals.append(ev)
                logger.info(
                    f"  🚀 BUY SIGNAL: {ev['name']} ({ticker})"
                    f"  RSI={ev['rsi']}  MACD={'GC' if ev['macd_crossover'] else 'GC接近'}"
                )
            else:
                logger.info(
                    f"  [{i+1}/{len(tickers)}] {ev['name']} ({ticker})"
                    f"  RSI={ev['rsi']}  MA25↑={ev['ma25_uptrend']}"
                )

            if (i + 1) % 20 == 0:
                storage.update_session_progress(scan_id, i + 1)

        except Exception as e:
            logger.error(f"[{i+1}/{len(tickers)}] {ticker} 評価エラー: {e}")

    storage.complete_session(
        scan_id=scan_id,
        buy_signal_count=len(buy_signals),
        sent_line=False,
    )
    logger.info(
        f"スキャン完了: BUY SIGNAL {len(buy_signals)} 件 / {len(all_results)} 銘柄処理"
    )
    return buy_signals


# ─────────────────────────────────────────────────────────────────────────────
def main():
    args = parse_args()

    # ── Web モード ──────────────────────────────────────────────────────────
    if args.web:
        import uvicorn
        logger.info("Web ダッシュボードサーバーを起動します...")
        uvicorn.run("web_app:app", host="0.0.0.0", port=8000, reload=True)
        return

    logger.info("STELLAR SCREENER スクリーニング処理を開始します...")

    # ── 設定読み込み ────────────────────────────────────────────────────────
    try:
        config = Config(args.config)
        logger.info(f"設定ファイルを読み込みました: {args.config}")
    except Exception as e:
        logger.error(f"設定の読み込みに失敗: {e}")
        sys.exit(1)

    # ── ティッカーリスト決定 ────────────────────────────────────────────────
    if args.jpx400:
        from screener.jpx400 import get_jpx400_tickers
        tickers = get_jpx400_tickers()
        scan_type = "jpx400"
        logger.info(f"JPX400 モード: {len(tickers)} 銘柄を対象にスキャンします。")
    else:
        tickers = config.tickers
        scan_type = "manual"
        if not tickers:
            logger.error("config.yaml に tickers が定義されていません。")
            sys.exit(1)
        logger.info(f"ウォッチリスト モード: {', '.join(tickers)}")

    # ── スクリーニング実行 ──────────────────────────────────────────────────
    buy_signals = run_screening(config, tickers, scan_type=scan_type)

    if not buy_signals:
        logger.info("BUY SIGNAL 該当なし。LINE 通知をスキップします。")
        return

    # ── BUY SIGNAL → LINE 通知 ──────────────────────────────────────────────
    notifier = LineNotifier(config.line_token, config.line_user_id)
    message  = notifier.build_buy_signal_message(buy_signals)

    if args.dry_run:
        logger.info("ドライラン: LINE には送信せず、コンソールに出力します。")
        print("\n=== BUY SIGNAL MESSAGE PREVIEW ===")
        print(message)
        print("===================================\n")
    else:
        if not config.validate_line_credentials():
            logger.error(
                "LINE 認証情報が未設定です。"
                ".env に LINE_CHANNEL_ACCESS_TOKEN と LINE_USER_ID を設定してください。"
                "（動作確認のみなら --dry-run を使用してください）"
            )
            sys.exit(1)

        success = notifier.send_notification(message)
        if success:
            logger.info("LINE 通知を正常に送信しました。処理完了。")
        else:
            logger.error("LINE 通知の送信に失敗しました。")
            sys.exit(1)


if __name__ == "__main__":
    main()
