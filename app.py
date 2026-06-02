"""윈도우 데스크톱 GUI (Tkinter).

실행:  python app.py   (또는 run_app.bat 더블클릭)

기능:
  - 엔진 시작/중지, 자동매매 ON/OFF, 즉시 스캔
  - 텔레그램 봇 시작/중지 (GUI와 동시 구동)
  - 대시보드: 계좌요약 + 보유종목 표
  - 시그널 현황, 수동 주문, 전체 청산
  - 설정 탭: 모드/텔레그램 chat_id 등 .env 저장
  - 실시간 로그 패널
표준 라이브러리 Tkinter만 사용 (추가 GUI 설치 불필요).
"""
import os
import queue
import threading
import logging
import tkinter as tk
from tkinter import ttk, messagebox

from src.config import load_settings, validate, update_env
from src.logger import get_logger
from src.trader import _format_balance_lines, _format_money, _to_float

log = get_logger("gui")

BG = "#1e1e2e"
FG = "#e0e0e0"
ACCENT = "#89b4fa"
GREEN = "#a6e3a1"
RED = "#f38ba8"
PANEL = "#27293d"


def mode_label(mode):
    return "실전" if mode == "real" else "모의"


def _clear_token_cache() -> bool:
    """계좌/모드 변경 시 기존 KIS 토큰 캐시를 삭제한다."""
    token_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "token.dat")
    if not os.path.exists(token_path):
        return False
    try:
        os.remove(token_path)
        log.info("KIS 토큰 캐시 삭제: %s", token_path)
        return True
    except OSError as e:
        log.warning("KIS 토큰 캐시 삭제 실패: %s", e)
        return False


class QueueLogHandler(logging.Handler):
    """로그 레코드를 GUI 큐로 전달."""
    def __init__(self, q):
        super().__init__()
        self.q = q

    def emit(self, record):
        try:
            self.q.put(("log", self.format(record)))
        except Exception:  # noqa
            pass


class TradingApp:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.trader = None
        self.bot = None
        self.settings = None

        root.title("한국투자증권 자동매매")
        root.geometry("920x680")
        root.configure(bg=BG)

        self._init_settings()
        self._build_ui()
        self._attach_log_handler()
        self.root.after(200, self._poll_queue)
        self._refresh_status()

    # ------------------------------------------------------------- #
    def _init_settings(self):
        try:
            self.settings = load_settings()
        except Exception as e:  # noqa
            messagebox.showerror("설정 오류", f"설정 로드 실패:\n{e}")

    def _attach_log_handler(self):
        h = QueueLogHandler(self.q)
        h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                                         datefmt="%H:%M:%S"))
        h.setLevel(logging.INFO)
        logging.getLogger("autotrader").addHandler(h)
        for n in ("kis", "trader", "telegram", "gui", "main", "backtest"):
            logging.getLogger(n).addHandler(h)

    # ------------------------------------------------------------- #
    #  UI 구성
    # ------------------------------------------------------------- #
    def _build_ui(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:  # noqa
            pass
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=PANEL, foreground=FG, padding=(14, 6))
        style.map("TNotebook.Tab", background=[("selected", ACCENT)],
                  foreground=[("selected", "#000000")])
        style.configure("Treeview", background=PANEL, foreground=FG,
                        fieldbackground=PANEL, rowheight=24)
        style.configure("Treeview.Heading", background=BG, foreground=ACCENT)

        # ---- 상단 컨트롤 바 ----
        top = tk.Frame(self.root, bg=BG)
        top.pack(fill="x", padx=12, pady=10)

        self.mode_lbl = tk.Label(top, text="●", bg=BG, fg=GREEN, font=("맑은 고딕", 14, "bold"))
        self.mode_lbl.pack(side="left")
        self.status_lbl = tk.Label(top, text="준비", bg=BG, fg=FG, font=("맑은 고딕", 11, "bold"))
        self.status_lbl.pack(side="left", padx=(6, 20))

        self.btn_engine = self._btn(top, "▶ 엔진 시작", self.toggle_engine, ACCENT)
        self.btn_mode = self._btn(top, "계좌 전환", self.switch_mode, GREEN)
        self.btn_auto = self._btn(top, "자동매매: --", self.toggle_auto, PANEL)
        self.btn_scan = self._btn(top, "🔍 즉시 스캔", self.scan_now, PANEL)
        self.btn_bot = self._btn(top, "🤖 봇 시작", self.toggle_bot, PANEL)

        self.mode_banner = tk.Label(self.root, text="", bg=ACCENT, fg="#000000",
                                    anchor="w", padx=10, pady=6,
                                    font=("맑은 고딕", 10, "bold"))
        self.mode_banner.pack(fill="x", padx=12, pady=(0, 8))

        # ---- 탭 ----
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        self._build_dashboard()
        self._build_signals()
        self._build_manual()
        self._build_settings()
        self._build_log()

        # ---- 상태바 ----
        self.bar = tk.Label(self.root, text="", bg=PANEL, fg=FG, anchor="w")
        self.bar.pack(fill="x", side="bottom")

    def _btn(self, parent, text, cmd, color):
        b = tk.Button(parent, text=text, command=cmd, bg=color, fg="#000000",
                      relief="flat", padx=10, pady=4, font=("맑은 고딕", 10, "bold"),
                      activebackground=ACCENT, cursor="hand2")
        b.pack(side="left", padx=4)
        return b

    def _build_dashboard(self):
        f = tk.Frame(self.nb, bg=BG)
        self.nb.add(f, text="📊 대시보드")

        summary = tk.Frame(f, bg=BG)
        summary.pack(fill="x", pady=8)
        self.cash_lbl = tk.Label(summary, text="예수금: -", bg=BG, fg=FG, font=("맑은 고딕", 12))
        self.cash_lbl.pack(side="left", padx=10)
        self.eval_lbl = tk.Label(summary, text="총평가: -", bg=BG, fg=FG, font=("맑은 고딕", 12))
        self.eval_lbl.pack(side="left", padx=10)
        tk.Button(summary, text="↻ 새로고침", command=self.refresh_portfolio, bg=PANEL,
                  fg=FG, relief="flat", cursor="hand2").pack(side="right", padx=10)

        cols = ("시장", "종목", "수량", "평단", "현재가", "평가손익", "수익률")
        self.tree = ttk.Treeview(f, columns=cols, show="headings", height=14)
        for c in cols:
            self.tree.heading(c, text=c)
            self.tree.column(c, anchor="center", width=105 if c != "종목" else 180)
        self.tree.pack(fill="both", expand=True, padx=8, pady=8)

    def _build_signals(self):
        f = tk.Frame(self.nb, bg=BG)
        self.nb.add(f, text="📡 시그널")
        tk.Button(f, text="↻ 시그널 갱신(스캔)", command=self.scan_now, bg=PANEL, fg=FG,
                  relief="flat", cursor="hand2").pack(anchor="e", padx=8, pady=6)
        cols = ("종목", "판정", "매수점수", "매도점수", "가격")
        self.sig_tree = ttk.Treeview(f, columns=cols, show="headings", height=15)
        for c in cols:
            self.sig_tree.heading(c, text=c)
            self.sig_tree.column(c, anchor="center", width=120)
        self.sig_tree.pack(fill="both", expand=True, padx=8, pady=8)

    def _build_manual(self):
        f = tk.Frame(self.nb, bg=BG)
        self.nb.add(f, text="🛒 수동 주문")
        box = tk.Frame(f, bg=PANEL)
        box.pack(padx=20, pady=20, fill="x")

        def row(label):
            r = tk.Frame(box, bg=PANEL); r.pack(fill="x", pady=6, padx=14)
            tk.Label(r, text=label, bg=PANEL, fg=FG, width=10, anchor="w").pack(side="left")
            return r

        r1 = row("종목코드")
        self.m_code = tk.Entry(r1, width=18); self.m_code.pack(side="left")
        tk.Label(r1, text="(국내 6자리 / 미국 심볼)", bg=PANEL, fg="#888").pack(side="left", padx=8)

        r2 = row("수량")
        self.m_qty = tk.Entry(r2, width=18); self.m_qty.insert(0, "1"); self.m_qty.pack(side="left")

        r3 = row("시장")
        self.m_market = ttk.Combobox(r3, values=["국내", "미국"], width=8, state="readonly")
        self.m_market.set("국내"); self.m_market.pack(side="left")
        self.m_exch = ttk.Combobox(r3, values=["NAS", "NYS", "AMS"], width=6, state="readonly")
        self.m_exch.set("NAS"); self.m_exch.pack(side="left", padx=8)

        r4 = tk.Frame(box, bg=PANEL); r4.pack(fill="x", pady=14, padx=14)
        tk.Button(r4, text="매수", command=lambda: self.manual_order("buy"), bg=RED,
                  fg="#000", relief="flat", width=12, font=("맑은 고딕", 11, "bold"),
                  cursor="hand2").pack(side="left", padx=6)
        tk.Button(r4, text="매도", command=lambda: self.manual_order("sell"), bg=ACCENT,
                  fg="#000", relief="flat", width=12, font=("맑은 고딕", 11, "bold"),
                  cursor="hand2").pack(side="left", padx=6)
        tk.Button(r4, text="⚠ 전체청산", command=self.liquidate, bg="#888",
                  fg="#000", relief="flat", width=16, cursor="hand2").pack(side="right", padx=6)

    def _build_settings(self):
        f = tk.Frame(self.nb, bg=BG)
        self.nb.add(f, text="⚙ 설정")
        box = tk.Frame(f, bg=PANEL); box.pack(padx=20, pady=20, fill="x")

        def row(label):
            r = tk.Frame(box, bg=PANEL); r.pack(fill="x", pady=8, padx=14)
            tk.Label(r, text=label, bg=PANEL, fg=FG, width=22, anchor="w").pack(side="left")
            return r

        s = self.settings
        r = row("거래 모드")
        self.set_mode = ttk.Combobox(r, values=["paper", "real"], width=12, state="readonly")
        self.set_mode.set(s.mode if s else "paper"); self.set_mode.pack(side="left")
        tk.Label(r, text="(paper=모의 / real=실전)", bg=PANEL, fg="#888").pack(side="left", padx=8)

        r = row("모의 계좌")
        self.set_paper_account = tk.Entry(r, width=24)
        self.set_paper_account.insert(0, s.paper_account if s else "")
        self.set_paper_account.pack(side="left")
        tk.Label(r, text="KIS_PAPER_ACCOUNT", bg=PANEL, fg="#888").pack(side="left", padx=8)

        r = row("실전 계좌")
        self.set_real_account = tk.Entry(r, width=24)
        self.set_real_account.insert(0, s.real_account if s else "")
        self.set_real_account.pack(side="left")
        tk.Label(r, text="KIS_REAL_ACCOUNT", bg=PANEL, fg=RED).pack(side="left", padx=8)

        r = row("텔레그램 chat_id")
        self.set_chat = tk.Entry(r, width=36)
        self.set_chat.insert(0, ",".join(s.allowed_chat_ids) if s else "")
        self.set_chat.pack(side="left")
        tk.Label(r, text="(쉼표로 여러 명, 본인만 봇 제어)", bg=PANEL, fg="#888").pack(side="left", padx=8)

        r = row("텔레그램 봇 토큰")
        self.set_token = tk.Entry(r, width=46, show="•")
        self.set_token.insert(0, s.telegram_token if s else "")
        self.set_token.pack(side="left")

        info = tk.Label(box, text="※ 앱키/시크릿은 보안상 .env 파일에서 직접 수정하세요. 계좌는 모의/실전으로 분리 저장됩니다.",
                        bg=PANEL, fg="#888", anchor="w")
        info.pack(fill="x", padx=14, pady=(4, 0))

        tk.Button(box, text="💾 저장 (.env 갱신 후 재시작 필요)", command=self.save_settings,
                  bg=GREEN, fg="#000", relief="flat", font=("맑은 고딕", 10, "bold"),
                  cursor="hand2").pack(pady=14)

        # 현재 설정 상태 표시
        self.cfg_status = tk.Label(box, text="", bg=PANEL, fg=FG, justify="left", anchor="w")
        self.cfg_status.pack(fill="x", padx=14, pady=6)
        self._update_cfg_status()

    def _build_log(self):
        f = tk.Frame(self.nb, bg=BG)
        self.nb.add(f, text="📜 로그")
        self.log_txt = tk.Text(f, bg="#11111b", fg="#cdd6f4", insertbackground=FG,
                               font=("Consolas", 9), wrap="word")
        self.log_txt.pack(fill="both", expand=True, padx=8, pady=8)
        self.log_txt.configure(state="disabled")

    # ------------------------------------------------------------- #
    #  설정 상태/검증
    # ------------------------------------------------------------- #
    def _update_cfg_status(self):
        if not self.settings:
            return
        missing = validate(self.settings)
        if missing:
            txt = "⚠ 누락: " + ", ".join(missing)
            color = RED
        else:
            txt = f"✅ 필수 설정 모두 입력됨 | 현재 적용 계좌: {self.settings.active_account_key}"
            color = GREEN
        self.cfg_status.config(text=txt, fg=color)

    def save_settings(self):
        new_mode = self.set_mode.get()
        if new_mode == "real" and self.settings and self.settings.mode != "real":
            if not messagebox.askyesno(
                    "⚠️ 실전투자 전환",
                    "real(실전) 모드는 실제 돈으로 주문이 나갈 수 있습니다.\n"
                    "모의 계좌와 실전 계좌가 분리되어 있는지 확인했습니다.\n정말 전환하시겠습니까?"):
                self.set_mode.set(self.settings.mode)
                return
        updates = {
            "TRADING_MODE": new_mode,
            "KIS_PAPER_ACCOUNT": self.set_paper_account.get().strip(),
            "KIS_REAL_ACCOUNT": self.set_real_account.get().strip(),
            "TELEGRAM_ALLOWED_CHAT_IDS": self.set_chat.get().strip(),
            "TELEGRAM_BOT_TOKEN": self.set_token.get().strip(),
        }
        try:
            update_env(updates)
            token_removed = _clear_token_cache()
            if self.trader and self.trader.running:
                self.trader.stop()
            if self.bot and self.bot.is_running:
                self.bot.stop()
            self.trader = None
            self.bot = None
            self.settings = load_settings()
            self._update_cfg_status()
            self.btn_engine.config(text="▶ 엔진 시작", bg=ACCENT)
            self.btn_bot.config(text="🤖 봇 시작", bg=PANEL)
            self._refresh_status()
            if token_removed:
                self._log("KIS 토큰 캐시 삭제됨 — 새 모드/계좌에서 재발급")
            messagebox.showinfo("저장 완료",
                                "설정을 저장했습니다.\n선택한 모드의 계좌가 새 엔진에 적용됩니다.")
            self._log(f"설정 저장됨 (.env, 모드={self.settings.mode}) — 엔진 재시작 필요")
        except Exception as e:  # noqa
            messagebox.showerror("저장 실패", str(e))

    def switch_mode(self):
        current = self.settings.mode
        new_mode = "real" if current != "real" else "paper"
        if new_mode == "real":
            if not messagebox.askyesno(
                    "⚠️ 실전 계좌로 전환",
                    "실전 계좌 화면으로 전환합니다.\n"
                    "엔진과 봇은 중지되고, 이후 주문은 KIS_REAL_ACCOUNT 기준으로 나갈 수 있습니다.\n"
                    "정말 실전으로 전환하시겠습니까?"):
                return
        try:
            update_env({"TRADING_MODE": new_mode})
            token_removed = _clear_token_cache()
            if self.trader and self.trader.running:
                self.trader.stop()
            if self.bot and self.bot.is_running:
                self.bot.stop()
            self.trader = None
            self.bot = None
            self.settings = load_settings()
            self.set_mode.set(self.settings.mode)
            self.btn_engine.config(text="▶ 엔진 시작", bg=ACCENT)
            self.btn_bot.config(text="🤖 봇 시작", bg=PANEL)
            self._update_cfg_status()
            self._refresh_status()
            if token_removed:
                self._log("KIS 토큰 캐시 삭제됨 — 새 모드/계좌에서 재발급")
            self._log(f"계좌 모드 전환: {current} → {new_mode}")
            messagebox.showinfo("전환 완료", f"{mode_label(new_mode)} 계좌 모드로 전환했습니다.\n엔진을 다시 시작하세요.")
        except Exception as e:  # noqa
            messagebox.showerror("전환 실패", str(e))

    # ------------------------------------------------------------- #
    #  엔진 제어
    # ------------------------------------------------------------- #
    def _ensure_trader(self):
        if self.trader is None:
            from src.trader import Trader
            self.trader = Trader(self.settings, notify=self._notify)
        return self.trader

    def toggle_engine(self):
        missing = validate(self.settings)
        if "APP_KEY" in missing or "ACCOUNT" in missing:
            messagebox.showwarning("설정 필요", "앱키/계좌를 먼저 .env 에 입력하세요.")
            return
        t = self._ensure_trader()
        if t.running:
            t.stop()
            self.btn_engine.config(text="▶ 엔진 시작", bg=ACCENT)
        else:
            t.start()
            self.btn_engine.config(text="⏹ 엔진 중지", bg=RED)
        self._refresh_status()

    def toggle_auto(self):
        t = self._ensure_trader()
        t.auto_enabled = not t.auto_enabled
        self._refresh_status()

    def scan_now(self):
        t = self._ensure_trader()
        self._log("즉시 스캔 시작...")
        threading.Thread(target=self._scan_worker, args=(t,), daemon=True).start()

    def _scan_worker(self, t):
        try:
            t.scan_once()
            self.q.put(("signals", dict(t.last_signals)))
            self.q.put(("log", "스캔 완료"))
        except Exception as e:  # noqa
            self.q.put(("log", f"스캔 오류: {e}"))

    def toggle_bot(self):
        missing = validate(self.settings)
        if "TELEGRAM_BOT_TOKEN" in missing or "TELEGRAM_ALLOWED_CHAT_IDS" in missing:
            messagebox.showwarning("설정 필요",
                                   "텔레그램 봇 토큰과 chat_id를 설정 탭에서 입력·저장 후 재시작하세요.")
            return
        t = self._ensure_trader()
        if self.bot and self.bot.is_running:
            self.bot.stop()
            self.btn_bot.config(text="🤖 봇 시작", bg=PANEL)
            self._log("텔레그램 봇 중지")
        else:
            from src.telegram_bot import TelegramController
            if self.bot is None:
                self.bot = TelegramController(self.settings, t)
                # 알림이 GUI와 텔레그램 양쪽으로 가도록 결합
                t.notify = self._notify
            self.bot.start_in_thread()
            self.btn_bot.config(text="🤖 봇 중지", bg=GREEN)
            self._log("텔레그램 봇 시작")
        self._refresh_status()

    # ------------------------------------------------------------- #
    #  주문
    # ------------------------------------------------------------- #
    def manual_order(self, side):
        code = self.m_code.get().strip().upper()
        if not code:
            messagebox.showwarning("입력", "종목코드를 입력하세요.")
            return
        try:
            qty = int(self.m_qty.get())
        except ValueError:
            messagebox.showwarning("입력", "수량은 숫자여야 합니다.")
            return
        overseas = self.m_market.get() == "미국"
        exch = self.m_exch.get()
        kind = "매수" if side == "buy" else "매도"
        if not messagebox.askyesno("주문 확인",
                                   f"[{self.settings.mode}] {code} {qty}주 {kind}?\n시장: {self.m_market.get()}"):
            return
        t = self._ensure_trader()

        def work():
            fn = t.manual_buy if side == "buy" else t.manual_sell
            res = fn(code, qty, overseas, exch)
            ok = "성공" if res.get("ok") else f"실패: {res.get('msg')}"
            self.q.put(("log", f"{kind} {code} {qty}주 → {ok}"))
            self.q.put(("popup", (f"{kind} 결과", f"{code} {qty}주: {ok}")))
        threading.Thread(target=work, daemon=True).start()

    def liquidate(self):
        if not messagebox.askyesno("전체 청산", "국내·미국 보유 종목을 모두 시장가로 청산할까요?"):
            return
        t = self._ensure_trader()

        def work():
            results = t.liquidate_all()
            if not results:
                self.q.put(("log", "청산할 보유 종목 없음"))
            else:
                self.q.put(("log", "청산: " + ", ".join(f"{c}({'OK' if ok else 'X'})" for c, ok in results)))
        threading.Thread(target=work, daemon=True).start()

    def refresh_portfolio(self):
        t = self._ensure_trader()

        def work():
            bal = t.portfolio_balance()
            self.q.put(("portfolio", bal))
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------- #
    #  큐 처리 / 상태 갱신
    # ------------------------------------------------------------- #
    def _notify(self, msg):
        """trader 알림 콜백: GUI 로그 + 텔레그램(봇 가동 시)."""
        self.q.put(("log", "🔔 " + msg))
        if self.bot and self.bot.is_running:
            try:
                self.bot.broadcast(msg)
            except Exception:  # noqa
                pass

    def _log(self, msg):
        self.q.put(("log", msg))

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "portfolio":
                    self._render_portfolio(payload)
                elif kind == "signals":
                    self._render_signals(payload)
                elif kind == "popup":
                    messagebox.showinfo(*payload)
        except queue.Empty:
            pass
        self.root.after(200, self._poll_queue)

    def _append_log(self, text):
        self.log_txt.configure(state="normal")
        self.log_txt.insert("end", text + "\n")
        self.log_txt.see("end")
        # 길이 제한
        if int(self.log_txt.index("end-1c").split(".")[0]) > 1000:
            self.log_txt.delete("1.0", "200.0")
        self.log_txt.configure(state="disabled")

    def _render_portfolio(self, bal):
        positions = bal.get("positions", [])
        has_usd = any(p.get("currency") == "USD" for p in positions) or bool(bal.get("cash_usd"))
        self.cash_lbl.config(
            text="예수금: " + _format_balance_lines(bal.get("cash_krw"), bal.get("cash_usd"), has_usd)
        )
        self.eval_lbl.config(
            text="총평가: " + _format_balance_lines(
                bal.get("total_eval_krw"), bal.get("total_eval_usd"), has_usd
            )
        )
        for i in self.tree.get_children():
            self.tree.delete(i)
        for p in positions:
            currency = p.get("currency", "KRW")
            self.tree.insert("", "end", values=(
                p.get("market_label", "-"), f"{p['name']}({p['code']})", p["qty"],
                _format_money(p["avg_price"], currency),
                _format_money(p["cur_price"], currency),
                _format_money(p.get("pnl_amt", 0), currency, signed=True),
                f"{_to_float(p.get('pnl_rate')):+.2f}%",
            ))

    def _render_signals(self, sigs):
        for i in self.sig_tree.get_children():
            self.sig_tree.delete(i)
        for code, s in sigs.items():
            emoji = {"buy": "📈매수", "sell": "📉매도", "hold": "⏸보유"}.get(s.action, s.action)
            self.sig_tree.insert("", "end", values=(
                code, emoji, f"{s.score_buy:.0f}", f"{s.score_sell:.0f}", f"{s.price:,.2f}"))

    def _refresh_status(self):
        s = self.settings
        running = self.trader and self.trader.running
        auto = self.trader and self.trader.auto_enabled
        bot_on = self.bot and self.bot.is_running
        if s and s.is_paper:
            self.mode_banner.config(text="모의투자 화면 | KIS_PAPER_ACCOUNT 적용", bg=ACCENT, fg="#000000")
            self.btn_mode.config(text="🔁 실전 계좌로 전환", bg=RED, fg="#000000")
        else:
            self.mode_banner.config(text="실전투자 화면 | KIS_REAL_ACCOUNT 적용 | 실제 주문 가능",
                                    bg=RED, fg="#000000")
            self.btn_mode.config(text="🔁 모의 계좌로 복귀", bg=GREEN, fg="#000000")
        self.mode_lbl.config(fg=GREEN if s and s.is_paper else RED)
        self.status_lbl.config(
            text=f"[{s.mode if s else '?'}] 엔진:{'가동' if running else '정지'}  "
                 f"봇:{'ON' if bot_on else 'OFF'}")
        self.btn_auto.config(text=f"자동매매: {'ON' if auto else 'OFF'}",
                             bg=GREEN if auto else PANEL)
        self.bar.config(text=f"모드 {s.mode if s else '?'} | "
                             f"감시 국내 {len(s.universe.get('domestic', [])) if s else 0} · "
                             f"해외 {len(s.universe.get('overseas', [])) if s else 0}")


def main():
    root = tk.Tk()
    TradingApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
