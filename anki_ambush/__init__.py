"""Ambush: pops up a small always-on-top window with a few due cards at random times."""

from __future__ import annotations

import json
import os
import random
import re
import subprocess
import sys
from datetime import date, datetime, time
from types import SimpleNamespace

import aqt
from anki.cards import Card
from anki.scheduler.v3 import QueuedCards
from aqt import gui_hooks, mw
from aqt.operations.scheduling import answer_card, bury_cards, suspend_cards
from aqt.qt import (
    sip,
    QAction,
    QCheckBox,
    QCursor,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QGuiApplication,
    QHBoxLayout,
    QKeySequence,
    QLabel,
    QMenu,
    QShortcut,
    QSpinBox,
    Qt,
    QTime,
    QTimeEdit,
    QTimer,
    QVBoxLayout,
    QWidget,
)
from aqt.reviewer import Reviewer, ReviewerBottomBar, V3CardInfo
from aqt.sound import av_player
from aqt.theme import theme_manager
from aqt.utils import tooltip
from aqt.webview import AnkiWebView, AnkiWebViewKind


ADDON_DIR = os.path.dirname(os.path.abspath(__file__))
# Installed read-only, e.g. via nixpkgs' anki.withAddons: config can't be
# written, so settings changes only last for the session.
CONFIG_READ_ONLY = not os.access(ADDON_DIR, os.W_OK)
_session_conf: dict | None = None


def cfg() -> dict:
    if _session_conf is not None:
        return dict(_session_conf)
    return mw.addonManager.getConfig(__name__) or {}


def save_cfg(conf: dict) -> None:
    global _session_conf
    if CONFIG_READ_ONLY:
        _session_conf = dict(conf)
    else:
        mw.addonManager.writeConfig(__name__, conf)


def card_range(conf: dict | None = None) -> tuple[int, int]:
    """`cards_per_popup` may be a number (3), a range string ("2-5") or a
    two-item list ([2, 5])."""
    val = (conf or cfg()).get("cards_per_popup", 3)
    try:
        if isinstance(val, str):
            parts = [int(p) for p in re.split(r"\s*[-–,]\s*", val.strip()) if p]
        elif isinstance(val, (list, tuple)):
            parts = [int(p) for p in val]
        else:
            parts = [int(val)]
        lo, hi = min(parts), max(parts)
    except (TypeError, ValueError):
        lo = hi = 3
    lo = max(1, lo)
    return lo, max(lo, hi)


# Card selection
##########################################################################


def next_card() -> tuple[QueuedCards, QueuedCards.QueuedCard] | None:
    """First card the scheduler would show next, optionally skipping new cards.
    Uses the currently selected deck, like the normal reviewer."""
    reviews_only = cfg().get("reviews_only", True)
    # don't show the card the main window is in the middle of reviewing
    main_card = mw.reviewer.card if mw.state == "review" else None
    skip_id = main_card.id if main_card else None
    queued = mw.col.sched.get_queued_cards(fetch_limit=50)
    for qc in queued.cards:
        if reviews_only and qc.queue == QueuedCards.NEW:
            continue
        if qc.card.id == skip_id:
            continue
        return queued, qc
    return None


class PickedCardInfo(V3CardInfo):
    """V3CardInfo whose 'top' card is the one we picked (which may not be the
    first in the queue when new cards are skipped)."""

    def __init__(self, queued: QueuedCards, qc: QueuedCards.QueuedCard) -> None:
        states = qc.states
        states.current.custom_data = qc.card.custom_data
        super().__init__(queued_cards=queued, states=states, context=qc.context)
        self._picked = qc

    def top_card(self) -> QueuedCards.QueuedCard:
        return self._picked


def invalidate_study_queue() -> None:
    """Drop the backend's cached study queue. Answering a card that isn't at
    the front of the queue fails with "not at top of queue": that happens when
    we picked a review past skipped new cards, skipped the main reviewer's
    card, or the main reviewer answered cards since we fetched ours. With no
    cached queue the backend skips that check and rebuilds the queue on the
    next fetch. Any non-undoable write does this, so touch an add-on-owned
    config key."""
    mw.col.conf.set("ambushQueueReset", 0)


# macOS overlay
##########################################################################

# NSWindowCollectionBehavior: canJoinAllSpaces | stationary | ignoresCycle | fullScreenAuxiliary
MAC_COLLECTION_BEHAVIOR = (1 << 0) | (1 << 4) | (1 << 6) | (1 << 8)
MAC_NONACTIVATING_PANEL = 1 << 7
MAC_STATUS_WINDOW_LEVEL = 25


def _objc():
    import ctypes
    import ctypes.util

    lib = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
    lib.sel_registerName.restype = ctypes.c_void_p
    lib.sel_registerName.argtypes = [ctypes.c_char_p]
    addr = ctypes.cast(lib.objc_msgSend, ctypes.c_void_p).value

    def send(obj, sel, *args, restype=ctypes.c_void_p, argtypes=()):
        fn = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p, *argtypes)(
            addr
        )
        return fn(obj, lib.sel_registerName(sel.encode()), *args)

    return ctypes, send


def make_mac_overlay(widget: QWidget, focus: bool) -> None:
    """Turn a shown Qt tool window into a floating, non-activating panel that sits
    above other apps (fullscreen ones included) on every Space."""
    if sys.platform != "darwin":
        return
    try:
        ctypes, send = _objc()
        win = send(int(widget.winId()), "window")
        if not win:
            return
        ul = ctypes.c_ulong
        mask = send(win, "styleMask", restype=ul)
        send(win, "setStyleMask:", ul(mask | MAC_NONACTIVATING_PANEL), argtypes=(ul,))
        send(win, "setLevel:", ctypes.c_long(MAC_STATUS_WINDOW_LEVEL), argtypes=(ctypes.c_long,))
        send(win, "setCollectionBehavior:", ul(MAC_COLLECTION_BEHAVIOR), argtypes=(ul,))
        send(win, "setHidesOnDeactivate:", False, argtypes=(ctypes.c_bool,))
        # stay up while Anki is hidden (Cmd+H), without unhiding its other windows
        send(win, "setCanHide:", False, argtypes=(ctypes.c_bool,))
        send(win, "orderFrontRegardless")
        if focus:
            # takes keystrokes without bringing Anki's other windows forward
            send(win, "makeKeyWindow")
    except Exception as e:
        print("anki ambush: mac overlay failed:", e)


_mac_activity = None


def keep_mac_timers_awake() -> None:
    """While Anki is hidden or in the background, App Nap can hold back its
    timers for a long time, which would delay ambushes. Opt out of it."""
    global _mac_activity
    if sys.platform != "darwin" or _mac_activity:
        return
    try:
        ctypes, send = _objc()
        objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        info = send(objc.objc_getClass(b"NSProcessInfo"), "processInfo")
        reason = send(
            objc.objc_getClass(b"NSString"),
            "stringWithUTF8String:",
            b"Anki Ambush timer",
            argtypes=(ctypes.c_char_p,),
        )
        # NSActivityUserInitiatedAllowingIdleSystemSleep: no App Nap, but the
        # Mac may still sleep
        opts = ctypes.c_uint64(0x00FFFFFF & ~(1 << 20))
        activity = send(
            info,
            "beginActivityWithOptions:reason:",
            opts,
            reason,
            argtypes=(ctypes.c_uint64, ctypes.c_void_p),
        )
        _mac_activity = send(activity, "retain")
    except Exception as e:
        print("anki ambush: couldn't disable App Nap:", e)


# Reviewer running inside the popup
##########################################################################


class BottomWeb(AnkiWebView):
    """Bottom bar webview; sizes itself to its content like Anki's own."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent, kind=AnkiWebViewKind.BOTTOM_TOOLBAR)
        self.disable_zoom()

    def _onHeight(self, qvar: int | None) -> None:
        if qvar:  # Anki's version resets the main window on failure
            self.setFixedHeight(int(qvar))


class AmbushReviewer(Reviewer):
    """Anki's own Reviewer, pointed at the popup's webviews instead of the main
    window. The card and the answer bar are rendered by the same code (with the
    same hooks and context objects) as the main screen, so note styling, the
    "show intervals" preference and add-ons that restyle or patch the reviewer
    apply here too."""

    def __init__(self, dialog: AmbushDialog, web: AnkiWebView, bottom: BottomWeb) -> None:
        super().__init__(mw)
        # super() registers a global audio hook for auto-advance; we don't use it
        gui_hooks.av_player_did_end_playing.remove(self._on_av_player_did_end_playing)
        self.dialog = dialog
        self.web = web
        self.bottom = SimpleNamespace(web=bottom)
        # popup's webviews can't run the custom-scheduling JS: Anki routes its
        # results to mw.reviewer
        self._state_mutation_js = None
        self._states_mutated = True
        web.set_bridge_command(self._linkHandler, self)
        bottom.set_bridge_command(self._linkHandler, ReviewerBottomBar(self))

    # flow

    def nextCard(self) -> None:
        d = self.dialog
        if d.answered >= d.total:
            d.close()
            return
        picked = next_card()
        if not picked:
            d.close()
            return
        self.previous_card = self.card
        self._v3 = PickedCardInfo(*picked)
        self.card = Card(mw.col, backend_card=self._v3.top_card().card)
        self.card.start_timer()
        if self._reps is None:
            self._initWeb()
        self._showQuestion()

    def _run_state_mutation_hook(self) -> None:
        pass

    def _showQuestion(self) -> None:
        self._reps += 1
        self.state = "question"
        self.typedAnswer = None
        c = self.card
        sounds = c.question_av_tags() if c.autoplay() else []
        self.web.setPlaybackRequiresGesture(not sounds)
        gui_hooks.reviewer_will_play_question_sounds(c, sounds)
        gui_hooks.av_player_will_play_tags(sounds, self.state, self)
        av_player.play_tags(sounds)

        q = gui_hooks.card_will_show(self._mungeQA(c.question()), c, "reviewQuestion")
        a = mw.col.media.escape_media_filenames(c.answer())
        bodyclass = theme_manager.body_classes_for_card_ord(c.ord)
        self.web.eval(
            f"_showQuestion({json.dumps(q)}, {json.dumps(a)}, '{bodyclass}');"
        )
        self._update_flag_icon()
        self._update_mark_icon()
        self._showAnswerButton()
        self.bottom.web.adjustHeightToFit()

    def _showAnswer(self) -> None:
        if self.state != "question" or not self.card:
            return
        self.state = "answer"
        c = self.card
        sounds = c.answer_av_tags() if c.autoplay() else []
        gui_hooks.reviewer_will_play_answer_sounds(c, sounds)
        gui_hooks.av_player_will_play_tags(sounds, self.state, self)
        av_player.play_tags(sounds)

        a = gui_hooks.card_will_show(self._mungeQA(c.answer()), c, "reviewAnswer")
        self.web.eval(f"_showAnswer({json.dumps(a)});")
        self._showEaseButtons()
        self.bottom.web.adjustHeightToFit()

    def _answerCard(self, ease) -> None:
        if self.state != "answer" or not self.card:
            return
        proceed, ease = gui_hooks.reviewer_will_answer_card((True, ease), self, self.card)
        if not proceed:
            return
        sched = mw.col.sched
        ans = sched.build_answer(
            card=self.card,
            states=self._v3.states,
            rating=self._v3.rating_from_ease(ease),
        )
        # always: the queue is shared with the main reviewer, so even a card
        # that was on top when fetched may not be any more
        invalidate_study_queue()

        def after_answer(_changes) -> None:
            self.card.load()
            suspended = self.card.queue < 0
            try:
                # lets stats/gamification add-ons count ambush reviews
                gui_hooks.reviewer_did_answer_card(self, self.card, ease)
            except Exception as e:
                print("anki ambush: reviewer_did_answer_card hook failed:", e)
            self._answeredIds.append(self.card.id)
            if sched.state_is_leech(ans.new_state):
                self.onLeech(suspended)
            self.dialog.answered += 1
            self.nextCard()

        self.state = "transition"
        answer_card(parent=mw, answer=ans).success(after_answer).run_in_background(
            initiator=self
        )

    def onEnterKey(self) -> None:
        if self.state == "question":
            self._getTypedAnswer()
        elif self.state == "answer":
            self._answerCard(self._defaultEase())

    def _remaining(self) -> str:
        d = self.dialog
        progress = f"<span class=ambush-progress>{d.answered + 1}/{d.total}</span>"
        counts = super()._remaining()
        return f"{counts} &nbsp;·&nbsp; {progress}" if counts else progress

    # links / menu

    def _linkHandler(self, url: str) -> None:
        if url == "edit":
            self.on_edit()
        elif url == "more":
            self.showContextMenu()
        else:
            super()._linkHandler(url)

    def on_edit(self) -> None:
        if self.card:
            aqt.dialogs.open(
                "Browser", mw, card=self.card, search=(f"cid:{self.card.id}",)
            )

    def showContextMenu(self) -> None:
        if not self.card or self.state == "transition":
            return
        m = QMenu(self.dialog)
        m.addAction("Edit", self.on_edit)
        m.addSeparator()
        m.addAction("Bury Card", self.bury_current_card)
        m.addAction("Suspend Card", self.suspend_current_card)
        m.addSeparator()
        m.addAction("Skip (Esc)", self.dialog.close)
        m.popup(QCursor.pos())

    def bury_current_card(self) -> None:
        if not self.card or self.state == "transition":
            return
        cid = self.card.id
        gui_hooks.reviewer_will_bury_card(cid)
        self._remove_current(bury_cards(parent=mw, card_ids=[cid]), "Card buried.")

    def suspend_current_card(self) -> None:
        if not self.card or self.state == "transition":
            return
        cid = self.card.id
        gui_hooks.reviewer_will_suspend_card(cid)
        self._remove_current(suspend_cards(parent=mw, card_ids=[cid]), "Card suspended.")

    def _remove_current(self, op, msg: str) -> None:
        self.state = "transition"

        def done(_res) -> None:
            tooltip(msg)
            self.dialog.answered += 1
            self.nextCard()

        op.success(done).run_in_background(initiator=self)

    def cleanup(self) -> None:
        # unlike Reviewer.cleanup, don't fire reviewer_will_end: the main
        # reviewer isn't ending
        self.card = None
        self.state = None


# Popup window
##########################################################################


class AmbushDialog(QDialog):
    def __init__(self, total: int, on_close) -> None:
        super().__init__(None)
        self.total = total
        self.answered = 0
        self.on_close_cb = on_close
        self._placing = False

        self.setWindowTitle("Anki Ambush")
        self.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)
        conf = cfg()
        self.resize(conf.get("width", 520), conf.get("height", 380))

        self.web = AnkiWebView(self, kind=AnkiWebViewKind.MAIN)
        self.bottom_web = BottomWeb(self)
        self.bottom_web.setFocusPolicy(Qt.FocusPolicy.WheelFocus)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.web, 1)
        layout.addWidget(self.bottom_web)

        self.reviewer = AmbushReviewer(self, self.web, self.bottom_web)
        r = self.reviewer

        def key(seq: str, fn) -> None:
            QShortcut(QKeySequence(seq), self, activated=fn)

        key("Space", r.onEnterKey)
        key("Return", r.onEnterKey)
        key("Enter", r.onEnterKey)
        for ease in range(1, 5):
            get_key = getattr(mw.pm, "get_answer_key", None)  # Anki 24.11+
            k = get_key(ease) if get_key else str(ease)
            if k:  # "" means the user disabled this answer's shortcut
                key(k, lambda ease=ease: r._answerCard(ease))
        key("E", r.on_edit)
        key("M", r.showContextMenu)
        key("R", r.replayAudio)
        key("F5", r.replayAudio)
        key("-", r.bury_current_card)
        key("@", r.suspend_current_card)
        key("Escape", self.close)

        self.place()
        r.nextCard()

    def place(self) -> None:
        """Snap to the bottom-right corner of the screen the mouse is on,
        shrinking the window if it wouldn't fit."""
        if self._placing:
            return
        self._placing = True
        screen = (
            QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        ).availableGeometry()
        margin = 24
        frame = self.frameGeometry()
        extra_w = frame.width() - self.width()
        extra_h = frame.height() - self.height()  # title bar
        w = min(self.width(), screen.width() - 2 * margin - extra_w)
        h = min(self.height(), screen.height() - 2 * margin - extra_h)
        if (w, h) != (self.width(), self.height()):
            self.resize(w, h)
        x = screen.right() + 1 - margin - w - extra_w
        y = screen.bottom() + 1 - margin - h - extra_h
        self.move(x, y)
        self._placing = False

    def showEvent(self, evt) -> None:
        super().showEvent(evt)
        self.place()

    def resizeEvent(self, evt) -> None:
        super().resizeEvent(evt)
        self.place()

    def closeEvent(self, evt) -> None:
        av_player.stop_and_clear_queue()
        self.reviewer.cleanup()
        self.web.cleanup()
        self.bottom_web.cleanup()
        cb, self.on_close_cb = self.on_close_cb, None
        if cb:
            cb(self.answered)
        super().closeEvent(evt)


# Scheduling of popups
##########################################################################


def mac_idle_seconds() -> float:
    if sys.platform != "darwin":
        return 0
    try:
        out = subprocess.run(
            ["ioreg", "-c", "IOHIDSystem", "-d", "4"],
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout
        m = re.search(r'"HIDIdleTime" = (\d+)', out)
        return int(m.group(1)) / 1e9 if m else 0
    except Exception:
        return 0


def parse_hhmm(s: str) -> time | None:
    try:
        h, m = s.split(":")
        return time(int(h), int(m))
    except Exception:
        return None


def in_quiet_hours() -> bool:
    conf = cfg()
    start = parse_hhmm(conf.get("quiet_hours_start", ""))
    end = parse_hhmm(conf.get("quiet_hours_end", ""))
    if not start or not end or start == end:
        return False
    now = datetime.now().time()
    if start < end:
        return start <= now < end
    return now >= start or now < end  # wraps past midnight


class Ambusher:
    def __init__(self) -> None:
        self.timer = QTimer(mw)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.check)
        self.dialog: AmbushDialog | None = None
        self.day = date.today()
        self.shown_today = 0
        self.dismissals = 0
        self.toggle_action: QAction | None = None

    # timer

    def schedule_in(self, minutes: float) -> None:
        self.timer.start(int(max(minutes, 0.1) * 60_000))

    def schedule_normal(self) -> None:
        conf = cfg()
        lo = conf.get("min_interval_minutes", 20)
        hi = max(lo, conf.get("max_interval_minutes", 60))
        mins = random.uniform(lo, hi)
        threshold = conf.get("backoff_after_dismissals", 3)
        if threshold and self.dismissals >= threshold:
            steps = self.dismissals - threshold + 1
            mult = min(
                conf.get("backoff_max_multiplier", 4),
                conf.get("backoff_factor", 2) ** steps,
            )
            mins *= mult
        self.schedule_in(mins)

    def start(self) -> None:
        keep_mac_timers_awake()
        if cfg().get("enabled", True):
            self.schedule_normal()

    def stop(self) -> None:
        self.timer.stop()
        d, self.dialog = self.dialog, None
        if d is not None and not sip.isdeleted(d):
            d.on_close_cb = None
            d.close()

    # firing

    def check(self, force: bool = False) -> None:
        if self.dialog is not None and sip.isdeleted(self.dialog):
            self.dialog = None
        if not mw.col or self.dialog:
            return
        conf = cfg()
        if not force:
            if not conf.get("enabled", True):
                return
            if date.today() != self.day:
                self.day, self.shown_today = date.today(), 0
            if in_quiet_hours():
                return self.schedule_in(10)
            cap = conf.get("daily_cap", 10)
            if cap and self.shown_today >= cap:
                return self.schedule_in(60)
            # don't pop up over an open modal dialog
            if mw.app.activeModalWidget():
                return self.schedule_in(3)
            idle_limit = conf.get("skip_when_idle_minutes", 5)
            if idle_limit and mac_idle_seconds() > idle_limit * 60:
                return self.schedule_in(1)
        if not next_card():
            if force:
                tooltip("Ambush: nothing due.")
            return self.schedule_normal()
        self.show()

    def show(self) -> None:
        self.shown_today += 1
        conf = cfg()
        self.dialog = AmbushDialog(random.randint(*card_range(conf)), self.on_closed)
        focus = conf.get("steal_focus", True)
        self.dialog.show()
        self.dialog.raise_()
        if sys.platform == "darwin":
            make_mac_overlay(self.dialog, focus)
        elif focus:
            self.dialog.activateWindow()

    def on_closed(self, answered: int) -> None:
        self.dialog = None
        self.dismissals = 0 if answered else self.dismissals + 1
        if cfg().get("enabled", True):
            self.schedule_normal()

    # settings

    def set_enabled(self, enabled: bool) -> None:
        conf = cfg()
        if conf.get("enabled", True) != enabled:
            conf["enabled"] = enabled
            save_cfg(conf)
        self.config_changed()

    def config_changed(self) -> None:
        """Re-sync the menu and the timer after the config was edited."""
        enabled = cfg().get("enabled", True)
        if self.toggle_action and self.toggle_action.isChecked() != enabled:
            self.toggle_action.blockSignals(True)
            self.toggle_action.setChecked(enabled)
            self.toggle_action.blockSignals(False)
        if not enabled:
            self.timer.stop()
        elif not self.dialog and mw.col:
            self.dismissals = 0
            self.schedule_normal()


ambusher = Ambusher()


# Settings panel
##########################################################################


class SettingsDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent or mw)
        self.setWindowTitle("Anki Ambush Settings")
        conf = cfg()

        def spin(lo: int, hi: int, val, suffix: str = "", special: str = "") -> QSpinBox:
            s = QSpinBox()
            s.setRange(lo, hi)
            s.setValue(int(val))
            if suffix:
                s.setSuffix(suffix)
            if special:
                s.setSpecialValueText(special)
            return s

        def row(*widgets) -> QWidget:
            w = QWidget()
            h = QHBoxLayout(w)
            h.setContentsMargins(0, 0, 0, 0)
            for x in widgets:
                if isinstance(x, str):
                    h.addWidget(QLabel(x))
                else:
                    h.addWidget(x)
            h.addStretch()
            return w

        def group(title: str) -> QFormLayout:
            g = QGroupBox(title)
            f = QFormLayout(g)
            outer.addWidget(g)
            return f

        outer = QVBoxLayout(self)

        if CONFIG_READ_ONLY:
            note = QLabel(
                "This add-on is installed read-only (e.g. by Nix), so changes "
                "here last until Anki restarts. To keep them, set them in your "
                "Nix config with <code>withConfig</code>."
            )
            note.setWordWrap(True)
            outer.addWidget(note)

        self.enabled = QCheckBox("Enable ambushes")
        self.enabled.setChecked(conf.get("enabled", True))
        outer.addWidget(self.enabled)

        # cards
        f = group("Cards")
        lo, hi = card_range(conf)
        self.cards_min = spin(1, 100, lo)
        self.cards_is_range = QCheckBox("random, up to")
        self.cards_is_range.setChecked(hi > lo)
        self.cards_max = spin(1, 100, hi)
        self.cards_max.setEnabled(hi > lo)
        self.cards_is_range.toggled.connect(self.cards_max.setEnabled)
        self.cards_min.valueChanged.connect(
            lambda v: self.cards_max.setValue(max(v, self.cards_max.value()))
        )
        f.addRow("Cards per ambush:", row(self.cards_min, self.cards_is_range, self.cards_max))
        self.reviews_only = QCheckBox("Only learning/review cards (no new cards)")
        self.reviews_only.setChecked(conf.get("reviews_only", True))
        f.addRow(self.reviews_only)

        # timing
        f = group("Timing")
        self.min_int = spin(1, 1440, conf.get("min_interval_minutes", 20), " min")
        self.max_int = spin(1, 1440, conf.get("max_interval_minutes", 60), " min")
        f.addRow("Wait between ambushes:", row(self.min_int, "to", self.max_int))
        self.daily_cap = spin(0, 1000, conf.get("daily_cap", 10), "", "No limit")
        f.addRow("Max ambushes per day:", self.daily_cap)

        start = parse_hhmm(conf.get("quiet_hours_start", ""))
        end = parse_hhmm(conf.get("quiet_hours_end", ""))
        self.quiet = QCheckBox("Quiet hours from")
        self.quiet.setChecked(bool(start and end))
        self.quiet_start = QTimeEdit(self._qtime(start or time(22, 0)))
        self.quiet_end = QTimeEdit(self._qtime(end or time(8, 0)))
        for t in (self.quiet_start, self.quiet_end):
            t.setDisplayFormat("HH:mm")
            t.setEnabled(self.quiet.isChecked())
            self.quiet.toggled.connect(t.setEnabled)
        f.addRow(row(self.quiet, self.quiet_start, "to", self.quiet_end))

        self.idle = spin(0, 240, conf.get("skip_when_idle_minutes", 5), " min", "Off")
        self.idle.setToolTip("macOS only: hold the popup while you've been away this long.")
        f.addRow("Wait while idle for:", self.idle)

        # backoff
        f = group("Back off when ignored")
        self.backoff_after = spin(0, 50, conf.get("backoff_after_dismissals", 3), "", "Never")
        f.addRow("After skipped ambushes in a row:", self.backoff_after)
        self.backoff_factor = QDoubleSpinBox()
        self.backoff_factor.setRange(1, 10)
        self.backoff_factor.setSingleStep(0.5)
        self.backoff_factor.setValue(float(conf.get("backoff_factor", 2)))
        self.backoff_factor.setPrefix("×")
        self.backoff_max = QDoubleSpinBox()
        self.backoff_max.setRange(1, 100)
        self.backoff_max.setValue(float(conf.get("backoff_max_multiplier", 4)))
        self.backoff_max.setPrefix("×")
        f.addRow("Grow wait by:", row(self.backoff_factor, "each time, at most", self.backoff_max))

        # window
        f = group("Window")
        self.width_ = spin(200, 4000, conf.get("width", 520), " px")
        self.height_ = spin(150, 4000, conf.get("height", 380), " px")
        f.addRow("Size:", row(self.width_, "×", self.height_))
        self.steal_focus = QCheckBox("Take keyboard focus when it appears")
        self.steal_focus.setChecked(conf.get("steal_focus", True))
        f.addRow(self.steal_focus)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    @staticmethod
    def _qtime(t: time) -> QTime:
        return QTime(t.hour, t.minute)

    def accept(self) -> None:
        conf = cfg()
        lo = self.cards_min.value()
        hi = max(lo, self.cards_max.value()) if self.cards_is_range.isChecked() else lo
        lo_int, hi_int = sorted((self.min_int.value(), self.max_int.value()))
        conf.update(
            enabled=self.enabled.isChecked(),
            cards_per_popup=lo if hi == lo else f"{lo}-{hi}",
            reviews_only=self.reviews_only.isChecked(),
            min_interval_minutes=lo_int,
            max_interval_minutes=hi_int,
            daily_cap=self.daily_cap.value(),
            quiet_hours_start=self.quiet_start.time().toString("HH:mm") if self.quiet.isChecked() else "",
            quiet_hours_end=self.quiet_end.time().toString("HH:mm") if self.quiet.isChecked() else "",
            skip_when_idle_minutes=self.idle.value(),
            backoff_after_dismissals=self.backoff_after.value(),
            backoff_factor=self.backoff_factor.value(),
            backoff_max_multiplier=self.backoff_max.value(),
            width=self.width_.value(),
            height=self.height_.value(),
            steal_focus=self.steal_focus.isChecked(),
        )
        save_cfg(conf)
        ambusher.config_changed()
        super().accept()


def open_settings() -> bool:
    SettingsDialog(mw).exec()
    return True  # tells the add-ons screen we handled the Config button


# Menu
##########################################################################


def setup_menu() -> None:
    menu = mw.form.menuTools.addMenu("Ambush")
    toggle = QAction("Enabled", mw, checkable=True)
    toggle.setChecked(cfg().get("enabled", True))
    toggle.toggled.connect(ambusher.set_enabled)
    menu.addAction(toggle)
    ambusher.toggle_action = toggle
    now = QAction("Ambush Now", mw)
    now.triggered.connect(lambda: ambusher.check(force=True))
    menu.addAction(now)
    menu.addSeparator()
    settings = QAction("Settings…", mw)
    settings.triggered.connect(open_settings)
    menu.addAction(settings)


setup_menu()
mw.addonManager.setConfigAction(__name__, open_settings)
mw.addonManager.setConfigUpdatedAction(__name__, lambda _conf: ambusher.config_changed())
gui_hooks.profile_did_open.append(ambusher.start)
gui_hooks.profile_will_close.append(ambusher.stop)
