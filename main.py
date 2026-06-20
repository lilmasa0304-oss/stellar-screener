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
        help="JPX400 全銘柄をスキャンする（config.yaml の universe:tickers でも自動判定）",
    )
    parser.add_argument(
        "--test-line",
        action="store_true",
        help="LINE 通知の接続テスト（テストメッセージを1通送信して終了）",
    )
    return parser.parse_args()


# ─────────────────────────────────────────────────────────────────────────────
def run_screening(config: Config, tickers: list, scan_type: str = "manual") -> tuple[list, str]:
    """
    指定 tickers を一括スクリーニングして、(BUY SIGNAL リスト, scan_id) を返す。
    スキャン結果は SQLite に保存される（セッション完了は呼び出し元で行う）。
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
                preset = ev.get("preset_matched", "none")
                logger.info(
                    f"  🚀 BUY SIGNAL: {ev['name']} ({ticker})"
                    f"  RSI={ev['rsi']}  preset={preset}"
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

    logger.info(
        f"スキャン完了: BUY SIGNAL {len(buy_signals)} 件 / {len(all_results)} 銘柄処理"
    )
    return buy_signals, scan_id


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

    if args.test_line:
        if not config.validate_line_credentials():
            logger.error(
                ".env に LINE_CHANNEL_ACCESS_TOKEN と LINE_USER_ID を設定してください。"
            )
            sys.exit(1)
        notifier = LineNotifier(config.line_token, config.line_user_id)
        sample = [{
            "ticker": "TEST.T",
            "name": "STELLAR SCREENER 接続テスト",
            "current_price": 1000.0,
            "change_percent": 0.0,
            "rsi": 30.0,
            "ma25": 990.0,
            "ma25_uptrend": True,
            "preset_matched": "oshieme",
            "reason": "LINE 通知連携のテストです。正常に受信できていれば設定は完了です。",
        }]
        message = notifier.build_buy_signal_message(sample)
        print("\n=== LINE TEST MESSAGE ===")
        print(message)
        print("=========================\n")
        if notifier.send_notification(message):
            logger.info("LINE 接続テスト: 送信成功")
            sys.exit(0)
        logger.error("LINE 接続テスト: 送信失敗")
        sys.exit(1)

    # ── ティッカーリスト決定 ────────────────────────────────────────────────
    if args.jpx400 or config.is_jpx400_universe():
        tickers = config.tickers
        scan_type = "jpx400"
        logger.info(f"JPX400 モード: {len(tickers)} 銘柄を対象にスキャンします。")
    else:
        tickers = config.tickers
        scan_type = "manual"
        if not tickers:
            logger.error("config.yaml に tickers が定義されていません。")
            sys.exit(1)
        logger.info(f"ウォッチリスト モード: {len(tickers)} 銘柄")

    # ── スクリーニング実行 ──────────────────────────────────────────────────
    buy_signals, scan_id = run_screening(config, tickers, scan_type=scan_type)

    sent_line = False
    if buy_signals:
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
                storage.complete_session(
                    scan_id=scan_id,
                    buy_signal_count=len(buy_signals),
                    sent_line=False,
                    error_message="LINE credentials not configured",
                )
                sys.exit(1)

            success = notifier.send_notification(message)
            if success:
                sent_line = True
                logger.info("LINE 通知を正常に送信しました。処理完了。")
            else:
                logger.error("LINE 通知の送信に失敗しました。")
                storage.complete_session(
                    scan_id=scan_id,
                    buy_signal_count=len(buy_signals),
                    sent_line=False,
                    error_message="LINE notification failed",
                )
                sys.exit(1)
    else:
        logger.info("BUY SIGNAL 該当なし。LINE 通知をスキップします。")

    storage.complete_session(
        scan_id=scan_id,
        buy_signal_count=len(buy_signals),
        sent_line=sent_line,
    )


if __name__ == "__main__":
    main()
