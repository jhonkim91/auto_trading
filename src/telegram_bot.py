"""텔레그램 봇 - 원격 제어 / 알림 / 리포트 / 수동 주문.

python-telegram-bot v20+ (async). 보안: 허용된 chat_id 화이트리스트.
명령:
  /start /help   - 도움말
  /status        - 엔진 상태
  /balance /portfolio - 잔고·보유 리포트
  /auto on|off   - 자동매매 on/off
  /run           - 지금 1회 스캔 실행
  /signals       - 최근 시그널 요약
  /buy  <코드> <수량> [us <거래소>]  - 수동 매수
  /sell <코드> <수량> [us <거래소>]  - 수동 매도
  /liquidate     - 국내 전체 청산
"""
import asyncio
import threading

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

from .logger import get_logger

log = get_logger("telegram")


class TelegramController:
    def __init__(self, settings, trader):
        self.s = settings
        self.trader = trader
        self.allowed = set(str(c) for c in settings.allowed_chat_ids)
        self.app = ApplicationBuilder().token(settings.telegram_token).build()
        self._loop = None
        self._thread = None
        self._register()
        # trader 알림 콜백을 텔레그램 전송으로 연결
        trader.notify = self._notify_sync

    # ------------------------------------------------------------- #
    def _register(self):
        h = self.app.add_handler
        h(CommandHandler(["start", "help"], self.cmd_help))
        h(CommandHandler("status", self.cmd_status))
        h(CommandHandler(["balance", "portfolio"], self.cmd_portfolio))
        h(CommandHandler("auto", self.cmd_auto))
        h(CommandHandler("run", self.cmd_run))
        h(CommandHandler("signals", self.cmd_signals))
        h(CommandHandler("buy", self.cmd_buy))
        h(CommandHandler("sell", self.cmd_sell))
        h(CommandHandler("liquidate", self.cmd_liquidate))

    # ------------------------------------------------------------- #
    #  보안: 화이트리스트 검증
    # ------------------------------------------------------------- #
    def _authorized(self, update: Update) -> bool:
        chat_id = str(update.effective_chat.id)
        if chat_id not in self.allowed:
            log.warning("미허가 접근 차단: chat_id=%s", chat_id)
            return False
        return True

    async def _guard(self, update: Update) -> bool:
        if not self._authorized(update):
            await update.message.reply_text("⛔ 권한이 없습니다.")
            return False
        return True

    # ------------------------------------------------------------- #
    #  알림 콜백 (다른 스레드의 trader 에서 호출됨)
    # ------------------------------------------------------------- #
    def _notify_sync(self, msg: str):
        if self._loop is None:
            log.info("[알림(루프 미준비)] %s", msg)
            return
        for chat_id in self.allowed:
            asyncio.run_coroutine_threadsafe(
                self.app.bot.send_message(chat_id=int(chat_id), text=msg), self._loop
            )

    # GUI 등 외부에서 호출하는 공개 알림 메서드
    def broadcast(self, msg: str):
        self._notify_sync(msg)

    @property
    def is_running(self) -> bool:
        return self._loop is not None and self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------- #
    #  명령 핸들러
    # ------------------------------------------------------------- #
    async def cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await self._guard(update):
            return
        await update.message.reply_text(
            "🤖 자동매매 봇 명령어\n"
            "/status - 엔진 상태\n"
            "/portfolio - 보유·평가손익\n"
            "/auto on|off - 자동매매 전환\n"
            "/run - 즉시 1회 스캔\n"
            "/signals - 최근 시그널\n"
            "/buy <코드> <수량> [us NAS] - 수동 매수\n"
            "/sell <코드> <수량> [us NAS] - 수동 매도\n"
            "/liquidate - 국내 전체 청산\n"
            f"\n현재 모드: {self.s.mode}"
        )

    async def cmd_status(self, update: Update, ctx):
        if not await self._guard(update):
            return
        t = self.trader
        await update.message.reply_text(
            f"📊 상태\n모드: {self.s.mode}\n"
            f"루프 실행중: {'예' if t.running else '아니오'}\n"
            f"자동매매: {'ON' if t.auto_enabled else 'OFF'}\n"
            f"매매중단(한도): {'예' if t.risk.halted else '아니오'}\n"
            f"감시 국내 {len(t._domestic_codes())}종목 / 해외 {len(t._overseas_items())}종목"
        )

    async def cmd_portfolio(self, update: Update, ctx):
        if not await self._guard(update):
            return
        await update.message.reply_text("조회 중...")
        report = await asyncio.to_thread(self.trader.portfolio_report)
        await update.message.reply_text(report)

    async def cmd_auto(self, update: Update, ctx):
        if not await self._guard(update):
            return
        arg = (ctx.args[0].lower() if ctx.args else "")
        if arg == "on":
            self.trader.auto_enabled = True
            await update.message.reply_text("✅ 자동매매 ON")
        elif arg == "off":
            self.trader.auto_enabled = False
            await update.message.reply_text("⏸️ 자동매매 OFF (신호 알림만)")
        else:
            await update.message.reply_text("사용법: /auto on  또는  /auto off")

    async def cmd_run(self, update: Update, ctx):
        if not await self._guard(update):
            return
        await update.message.reply_text("🔍 1회 스캔 실행...")
        await asyncio.to_thread(self.trader.scan_once)
        await update.message.reply_text("스캔 완료")

    async def cmd_signals(self, update: Update, ctx):
        if not await self._guard(update):
            return
        sigs = self.trader.last_signals
        if not sigs:
            await update.message.reply_text("아직 평가된 시그널이 없습니다. /run 으로 실행하세요.")
            return
        lines = ["📡 최근 시그널"]
        for code, s in sigs.items():
            emoji = {"buy": "📈", "sell": "📉", "hold": "⏸️"}.get(s.action, "")
            lines.append(f"{emoji} {code}: {s.action} (매수{s.score_buy:.0f}/매도{s.score_sell:.0f}) @ {s.price:,.2f}")
        await update.message.reply_text("\n".join(lines))

    def _parse_order_args(self, args):
        """<코드> <수량> [us <거래소>] 파싱."""
        if len(args) < 2:
            return None
        code = args[0].upper()
        qty = int(args[1])
        overseas = len(args) >= 3 and args[2].lower() == "us"
        exchange = args[3].upper() if overseas and len(args) >= 4 else "NAS"
        return code, qty, overseas, exchange

    async def cmd_buy(self, update: Update, ctx):
        if not await self._guard(update):
            return
        parsed = self._parse_order_args(ctx.args)
        if not parsed:
            await update.message.reply_text("사용법: /buy 005930 10  또는  /buy AAPL 1 us NAS")
            return
        code, qty, overseas, exch = parsed
        res = await asyncio.to_thread(self.trader.manual_buy, code, qty, overseas, exch)
        ok = "✅성공" if res.get("ok") else f"❌실패: {res.get('msg')}"
        await update.message.reply_text(f"매수 {code} {qty}주 → {ok}")

    async def cmd_sell(self, update: Update, ctx):
        if not await self._guard(update):
            return
        parsed = self._parse_order_args(ctx.args)
        if not parsed:
            await update.message.reply_text("사용법: /sell 005930 10  또는  /sell AAPL 1 us NAS")
            return
        code, qty, overseas, exch = parsed
        res = await asyncio.to_thread(self.trader.manual_sell, code, qty, overseas, exch)
        ok = "✅성공" if res.get("ok") else f"❌실패: {res.get('msg')}"
        await update.message.reply_text(f"매도 {code} {qty}주 → {ok}")

    async def cmd_liquidate(self, update: Update, ctx):
        if not await self._guard(update):
            return
        await update.message.reply_text("⚠️ 국내 전체 청산 실행...")
        results = await asyncio.to_thread(self.trader.liquidate_all)
        if not results:
            await update.message.reply_text("청산할 보유 종목이 없습니다.")
            return
        txt = "\n".join(f"{c}: {'✅' if ok else '❌'}" for c, ok in results)
        await update.message.reply_text("청산 결과\n" + txt)

    # ------------------------------------------------------------- #
    async def _post_init(self, app):
        self._loop = asyncio.get_running_loop()
        for chat_id in self.allowed:
            try:
                await app.bot.send_message(
                    chat_id=int(chat_id),
                    text=f"🚀 자동매매 봇 가동 (모드: {self.s.mode}). /help 로 명령어 확인",
                )
            except Exception:  # noqa
                pass

    def run(self):
        """봇 실행(블로킹, CLI 용). 메인 스레드에서 호출."""
        self.app.post_init = self._post_init
        self.app.run_polling(allowed_updates=Update.ALL_TYPES)

    def start_in_thread(self):
        """봇을 별도 스레드에서 실행 (GUI 용). 신호 핸들러 비활성화."""
        if self.is_running:
            return False

        def _worker():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self.app.post_init = self._post_init
            try:
                # stop_signals=None: 메인스레드가 아니므로 시그널 핸들러 비활성화
                self.app.run_polling(
                    allowed_updates=Update.ALL_TYPES,
                    stop_signals=None,
                    close_loop=False,
                )
            except Exception:  # noqa
                log.exception("텔레그램 봇 스레드 오류")

        self._thread = threading.Thread(target=_worker, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        """봇 정지 (이벤트 루프에 종료 요청)."""
        if self._loop and self.app.running:
            asyncio.run_coroutine_threadsafe(self.app.stop(), self._loop)
        self._loop = None
