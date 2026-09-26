"""The Board window: Trello-style columns of cards, with due dates.

  * "+ Add another column" at the end of the row; a column's name can be double-clicked to rename it,
    and its "..." menu moves or deletes it.
  * "+ Add a card" at the bottom of each column opens a small box: type, Enter, type the next one.
    A day at the end sets the due date ("pay rent friday"), and a time too ("dinner with Mom at 4pm tomorrow").
  * Cards drag between columns (and up and down within one). Double-click a card to edit its title and
    due date and time; right-click for quick due dates, moving and deleting.

Everything is stored by board.py, which voice commands use too; `controller.board_changed` redraws
this window when one of them changes something.
"""

from __future__ import annotations

from datetime import date, time as dtime

from PySide6.QtCore import (QDate, QEasingCurve, QEvent, QPoint, QRect, QSize, Qt, QTime, QTimer, QVariantAnimation,
                            Signal)
from PySide6.QtGui import QColor, QCursor, QDrag, QFont, QFontMetrics, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDateEdit, QDialog, QFrame, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QMenu, QMessageBox, QPushButton,
                               QScrollArea, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QTimeEdit,
                               QToolButton, QVBoxLayout, QWidget)

import assistant as backend
import board

from . import frame
from .motion import animations_enabled
from .theme import COLORS, enable_dark_title_bar
from .theme import signals as theme_signals

COLUMN_WIDTH = 272
WHEEL_STEP_SIDEWAYS = 150        # pixels per wheel notch across the board
WHEEL_STEP_CARDS = 90            # ...and down a column
SCROLL_MS = 180


class SmoothScroll:
    """Eases a scroll bar to where the wheel sends it instead of jumping a notch at a time. Notches that
    arrive mid-glide add to the destination, so a fast spin keeps going smoothly."""

    def __init__(self, bar):
        self.bar = bar
        self.target = bar.value()
        self.anim = QVariantAnimation(bar)
        self.anim.setDuration(SCROLL_MS)
        self.anim.setEasingCurve(QEasingCurve.OutCubic)
        self.anim.valueChanged.connect(lambda v: self.bar.setValue(int(v)))

    @classmethod
    def of(cls, bar) -> "SmoothScroll":
        smooth = getattr(bar, "_smooth", None)
        if smooth is None:
            smooth = bar._smooth = cls(bar)
        return smooth

    def by(self, pixels: float, animate: bool = True) -> None:
        start = self.target if self.anim.state() == QVariantAnimation.Running else self.bar.value()
        self.target = int(max(self.bar.minimum(), min(self.bar.maximum(), start + pixels)))
        if not animate or not animations_enabled():
            self.anim.stop()
            self.bar.setValue(self.target)
            return
        self.anim.stop()
        self.anim.setStartValue(self.bar.value())
        self.anim.setEndValue(self.target)
        self.anim.start()
CARD_ROLE = Qt.UserRole          # the card dict on each list item
PAD = 10                         # inside a card tile
BADGE_H = 20


# ---------------------------------------------------------------------------
# A card, drawn as a tile
# ---------------------------------------------------------------------------

class CardDelegate(QStyledItemDelegate):
    """Draws each card as a rounded tile: its title (wrapped) and, if it has one, a due-date badge
    coloured by urgency (overdue, today, within two days, later)."""

    def _title_font(self, option) -> QFont:
        font = QFont(option.font)
        font.setPointSizeF(max(9.0, font.pointSizeF() + 0.5))
        return font

    def _title_rect(self, option, width: int, text: str) -> QRect:
        fm = QFontMetrics(self._title_font(option))
        return fm.boundingRect(QRect(0, 0, max(40, width - 2 * PAD), 10_000), Qt.TextWordWrap, text)

    def card_width(self) -> int:
        view = self.parent()
        if isinstance(view, QListWidget) and view.viewport().width() > 60:
            return view.viewport().width() - 2 * view.spacing()
        return COLUMN_WIDTH - 24

    def sizeHint(self, option, index) -> QSize:
        card = index.data(CARD_ROLE) or {}
        width = self.card_width()
        height = self._title_rect(option, width, card.get("title", "")).height() + 2 * PAD
        if card.get("due"):
            height += BADGE_H + 6
        return QSize(width, height)

    def paint(self, painter: QPainter, option, index) -> None:
        card = index.data(CARD_ROLE) or {}
        c = COLORS
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        rect = option.rect.adjusted(1, 1, -1, -1)
        selected = bool(option.state & QStyle.State_Selected)
        hover = bool(option.state & QStyle.State_MouseOver)
        painter.setPen(QPen(QColor(c["accent"] if selected else c["accent_dim"] if hover else c["border"]),
                            2 if selected else 1))
        painter.setBrush(QColor(c["panel_alt"]))
        painter.drawRoundedRect(rect, 8, 8)

        painter.setFont(self._title_font(option))
        painter.setPen(QColor(c["text"]))
        title_rect = QRect(rect.left() + PAD, rect.top() + PAD, rect.width() - 2 * PAD, rect.height())
        painter.drawText(title_rect, Qt.TextWordWrap | Qt.AlignLeft | Qt.AlignTop, card.get("title", ""))

        text = board.describe_due(card.get("due"))
        if text:
            state = board.due_state(card.get("due"))
            fill = {"overdue": c["error"], "today": c["accent"]}.get(state)
            small = QFont(option.font)
            small.setPointSizeF(max(8.0, small.pointSizeF() - 0.5))
            painter.setFont(small)
            label = f"\U0001f4c5 {text}"
            width = min(QFontMetrics(small).horizontalAdvance(label) + 14, rect.width() - 2 * PAD)
            badge = QRect(rect.left() + PAD, rect.bottom() - PAD - BADGE_H + 2, width, BADGE_H)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(fill) if fill else QColor(c["panel"]))
            painter.drawRoundedRect(badge, 5, 5)
            painter.setPen(QColor(c.get("on_accent", "#000")) if fill else
                           QColor(c["accent_bright"] if state == "soon" else c["muted"]))
            painter.drawText(badge, Qt.AlignCenter, label)
        painter.restore()


class CardList(QListWidget):
    """One column's cards. Drags between columns are applied to board.py, then the window redraws
    from it (so what you see is always what is stored)."""

    changed = Signal()
    picked = Signal()            # a card here was selected: the other columns drop their selection

    def __init__(self, column_id: str):
        super().__init__()
        self.column_id = column_id
        self.itemSelectionChanged.connect(lambda: self.picked.emit() if self.selectedItems() else None)
        self.setItemDelegate(CardDelegate(self))
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setResizeMode(QListWidget.Adjust)
        self.setSpacing(3)
        self.setMouseTracking(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setAccessibleName("Cards")
        self.setMinimumHeight(40)

    def sizeHint(self) -> QSize:
        rows = sum(self.sizeHintForRow(i) + 2 * self.spacing() for i in range(self.count()))
        return QSize(COLUMN_WIDTH - 16, max(40, rows + 4))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.doItemsLayout()                     # the cards follow the list's width (and re-wrap)...
        hint = self.sizeHint()
        if hint != getattr(self, "_last_hint", None):
            self._last_hint = hint
            self.updateGeometry()                # ...and the column grows to fit them

    def keyPressEvent(self, event) -> None:
        item = self.currentItem()
        if event.key() == Qt.Key_Delete and item is not None and item.isSelected():
            board.delete_card(item.data(CARD_ROLE)["id"])
            self.changed.emit()
            return
        super().keyPressEvent(event)

    def deselect(self) -> None:
        self.blockSignals(True)
        self.clearSelection()
        self.setCurrentRow(-1)
        self.blockSignals(False)
        self.viewport().update()

    def card_pixmap(self, item) -> QPixmap:
        """The card as it looks on screen, whole: what the pointer carries while dragging. (Qt's own
        drag image clipped the right side of the tile.)"""
        rect = self.visualItemRect(item)
        ratio = self.devicePixelRatioF()
        pixmap = QPixmap(rect.size() * ratio)
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(Qt.transparent)
        option = QStyleOptionViewItem()
        self.initViewItemOption(option)
        option.rect = QRect(QPoint(0, 0), rect.size())
        option.state |= QStyle.State_Selected
        painter = QPainter(pixmap)
        painter.setOpacity(0.92)
        self.itemDelegate().paint(painter, option, self.indexFromItem(item))
        painter.end()
        return pixmap

    def startDrag(self, _actions) -> None:
        item = self.currentItem()
        if item is None:
            return
        rect = self.visualItemRect(item)
        drag = QDrag(self)
        drag.setMimeData(self.mimeData([item]))
        drag.setPixmap(self.card_pixmap(item))
        grab = self.viewport().mapFromGlobal(QCursor.pos()) - rect.topLeft()
        drag.setHotSpot(QPoint(max(0, min(grab.x(), rect.width())), max(0, min(grab.y(), rect.height()))))
        drag.exec(Qt.MoveAction)                 # dropEvent applies the move; the source keeps its item

    def dragEnterEvent(self, event) -> None:
        if isinstance(event.source(), CardList):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if isinstance(event.source(), CardList):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        source = event.source()
        item = source.currentItem() if isinstance(source, CardList) else None
        if item is None:
            event.ignore()
            return
        target = self.itemAt(event.position().toPoint())
        index = self.row(target) if target is not None else self.count()
        if target is not None and event.position().toPoint().y() > self.visualItemRect(target).center().y():
            index += 1                                           # dropped on the lower half: after it
        board.move_card(item.data(CARD_ROLE)["id"], self.column_id, index)
        event.setDropAction(Qt.CopyAction)       # nothing for Qt to remove: the redraw shows the new order
        event.accept()
        QTimer.singleShot(0, self.changed.emit)


# ---------------------------------------------------------------------------
# Editing one card
# ---------------------------------------------------------------------------

class CardDialog(QDialog):
    def __init__(self, card: dict, parent=None):
        super().__init__(parent)
        self.card = card
        self.deleted = False
        self.setWindowTitle("Edit card")
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)
        layout.addWidget(QLabel("Title"))
        self.title = QLineEdit(card.get("title", ""))
        self.title.setMaxLength(board.TITLE_MAX)
        self.title.setAccessibleName("Card title")
        layout.addWidget(self.title)
        due_row = QHBoxLayout()
        self.has_due = QCheckBox("Due date")
        self.date = QDateEdit()
        self.date.setCalendarPopup(True)
        self.date.setDisplayFormat("ddd d MMM yyyy")
        self.date.setAccessibleName("Due date")
        due = board._parse_iso(card["due"]) if card.get("due") else None
        self.has_due.setChecked(due is not None)
        shown = due or date.today()
        self.date.setDate(QDate(shown.year, shown.month, shown.day))
        self.date.setEnabled(due is not None)
        due_row.addWidget(self.has_due)
        due_row.addWidget(self.date, 1)
        layout.addLayout(due_row)
        time_row = QHBoxLayout()
        at = board.due_time(card.get("due"))
        self.has_time = QCheckBox("At a time")
        self.has_time.setChecked(at is not None)
        self.time = QTimeEdit()
        self.time.setDisplayFormat("HH:mm" if board.CLOCK["24h"]() else "h:mm AP")
        self.time.setAccessibleName("Due time")
        shown_at = at or dtime(9, 0)
        self.time.setTime(QTime(shown_at.hour, shown_at.minute))
        time_row.addWidget(self.has_time)
        time_row.addWidget(self.time, 1)
        layout.addLayout(time_row)
        self.has_due.toggled.connect(self._sync_due_fields)
        self.has_time.toggled.connect(self._sync_due_fields)
        self._sync_due_fields()
        buttons = QHBoxLayout()
        delete = QPushButton("Delete card")
        delete.clicked.connect(self._delete)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        save = QPushButton("Save")
        save.setObjectName("primary")
        save.setDefault(True)
        save.clicked.connect(self._save)
        buttons.addWidget(delete)
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        layout.addLayout(buttons)
        c = COLORS
        self.setStyleSheet(f"QLineEdit, QDateEdit, QTimeEdit {{ background: {c['panel_alt']}; border: 1px solid {c['border']}; "
                           f"border-radius: 8px; padding: 6px 8px; }}")
        enable_dark_title_bar(self)
        frame.install(self, minimize=False, maximize=False)   # NEON's title bar, or the native one (Settings > Appearance)

    def _sync_due_fields(self, *_args) -> None:
        """No date: no time either (greyed out, as Settings does)."""
        self.date.setEnabled(self.has_due.isChecked())
        self.has_time.setEnabled(self.has_due.isChecked())
        self.time.setEnabled(self.has_due.isChecked() and self.has_time.isChecked())

    def due_value(self) -> str | None:
        if not self.has_due.isChecked():
            return None
        d, t = self.date.date(), self.time.time()
        at = dtime(t.hour(), t.minute()) if self.has_time.isChecked() else None
        return board.make_due(date(d.year(), d.month(), d.day()), at)

    def _save(self) -> None:
        if not self.title.text().strip():
            self.title.setFocus()
            return
        board.update_card(self.card["id"], title=self.title.text(), due=self.due_value())
        self.accept()

    def _delete(self) -> None:
        board.delete_card(self.card["id"])
        self.deleted = True
        self.accept()


# ---------------------------------------------------------------------------
# A column
# ---------------------------------------------------------------------------

class ColumnWidget(QFrame):
    changed = Signal()

    def __init__(self, column: dict, index: int, total: int, window: "BoardWindow"):
        super().__init__()
        self.column = column
        self._window = window
        self.setObjectName("column")
        self.setFixedWidth(COLUMN_WIDTH)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        head = QHBoxLayout()
        self.name = QLabel(column["name"])
        self.name.setObjectName("columnName")
        self.name.setToolTip("Double-click to rename")
        self.name.mouseDoubleClickEvent = lambda _e: self.start_rename()
        self.name_edit = QLineEdit(column["name"])
        self.name_edit.setMaxLength(board.COLUMN_MAX)
        self.name_edit.hide()
        self.name_edit.returnPressed.connect(self._finish_rename)
        self.name_edit.editingFinished.connect(self._finish_rename)
        self.count = QLabel(str(len(column["cards"])) if column["cards"] else "")
        self.count.setObjectName("muted")
        more = QToolButton()
        more.setText("⋯")
        more.setAccessibleName(f"{column['name']} column menu")
        more.setPopupMode(QToolButton.InstantPopup)
        more.setAutoRaise(True)
        menu = QMenu(more)
        menu.addAction("Rename").triggered.connect(self.start_rename)
        left = menu.addAction("Move left")
        left.setEnabled(index > 0)
        left.triggered.connect(lambda: self._move(index - 1))
        right = menu.addAction("Move right")
        right.setEnabled(index < total - 1)
        right.triggered.connect(lambda: self._move(index + 1))
        menu.addSeparator()
        menu.addAction("Delete column...").triggered.connect(self._delete)
        more.setMenu(menu)
        head.addWidget(self.name, 1)
        head.addWidget(self.name_edit, 1)
        head.addWidget(self.count)
        head.addWidget(more)
        layout.addLayout(head)

        self.cards = CardList(column["id"])
        for card in column["cards"]:
            item = QListWidgetItem()
            item.setData(CARD_ROLE, card)
            item.setData(Qt.AccessibleTextRole, card["title"] + (f", due {board.describe_due(card['due'])}"
                                                                  if card.get("due") else ""))
            self.cards.addItem(item)
        self.cards.changed.connect(self.changed)
        self.cards.itemDoubleClicked.connect(lambda item: window.edit_card(item.data(CARD_ROLE)))
        self.cards.setContextMenuPolicy(Qt.CustomContextMenu)
        self.cards.customContextMenuRequested.connect(self._card_menu)
        layout.addWidget(self.cards)

        # "+ Add a card" turns into a box; Enter adds and keeps it open for the next one, Esc closes it.
        self.add_btn = QPushButton("+  Add a card")
        self.add_btn.setObjectName("addCard")
        self.add_btn.clicked.connect(self.open_adder)
        self.adder = QWidget()
        adder = QVBoxLayout(self.adder)
        adder.setContentsMargins(0, 0, 0, 0)
        adder.setSpacing(6)
        self.new_title = QLineEdit()
        self.new_title.setPlaceholderText("Card title (end with a day to set a due date: \"pay rent friday\")")
        self.new_title.setMaxLength(board.TITLE_MAX)
        self.new_title.setAccessibleName("New card title")
        self.new_title.returnPressed.connect(self._add_card)
        self.new_title.installEventFilter(self)
        row = QHBoxLayout()
        ok = QPushButton("Add card")
        ok.setObjectName("primary")
        ok.clicked.connect(self._add_card)
        close = QPushButton("Cancel")
        close.setToolTip("Stop adding cards (Esc)")
        close.clicked.connect(self.close_adder)
        row.addWidget(ok)
        row.addWidget(close)
        row.addStretch(1)
        adder.addWidget(self.new_title)
        adder.addLayout(row)
        self.adder.hide()
        layout.addWidget(self.adder)
        layout.addWidget(self.add_btn)
        self.setSizePolicy(self.sizePolicy().horizontalPolicy(), self.sizePolicy().Policy.Maximum)

    # ---- adding cards ----------------------------------------------------------------------------
    def open_adder(self) -> None:
        self.add_btn.hide()
        self.adder.show()
        self.new_title.setFocus()

    def close_adder(self) -> None:
        self.new_title.clear()
        self.adder.hide()
        self.add_btn.show()

    def eventFilter(self, obj, event) -> bool:
        if obj is self.new_title and event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key_Escape:
            self.close_adder()
            return True
        return super().eventFilter(obj, event)

    def _add_card(self) -> None:
        text = self.new_title.text().strip()
        if not text:
            return
        title, when = board.split_due(text)
        board.add_card(self.column["id"], title, when)
        self._window.reload(keep_adder=self.column["id"])

    # ---- the column itself ------------------------------------------------------------------------
    def start_rename(self) -> None:
        self.name.hide()
        self.name_edit.setText(self.column["name"])
        self.name_edit.show()
        self.name_edit.setFocus()
        self.name_edit.selectAll()

    def _finish_rename(self) -> None:
        if self.name_edit.isHidden():
            return
        self.name_edit.hide()
        self.name.show()
        new = self.name_edit.text().strip()
        if new and new != self.column["name"]:
            board.rename_column(self.column["id"], new)
            QTimer.singleShot(0, self.changed.emit)

    def _move(self, index: int) -> None:
        board.move_column(self.column["id"], index)
        self.changed.emit()

    def _delete(self) -> None:
        count = len(self.column["cards"])
        if count:
            answer = QMessageBox.question(
                self, "Delete column", f"Delete \"{self.column['name']}\" and its {count} "
                                       f"card{'s' if count != 1 else ''}? This can't be undone.")
            if answer != QMessageBox.Yes:
                return
        board.delete_column(self.column["id"])
        self.changed.emit()

    def _card_menu(self, pos) -> None:
        item = self.cards.itemAt(pos)
        if item is None:
            return
        card = item.data(CARD_ROLE)
        menu = QMenu(self)
        menu.addAction("Edit...").triggered.connect(lambda: self._window.edit_card(card))
        due = menu.addMenu("Due date")
        today = date.today()
        for label, spoken in (("Today", "today"), ("Tomorrow", "tomorrow"), ("End of the week", "end of the week"),
                              ("Next week", "next week")):
            when = board.parse_due(spoken, today)
            due.addAction(label).triggered.connect(     # a new day keeps the card's time
                lambda _c=False, w=when: self._set_due(card, board.make_due(w, board.due_time(card.get("due")))))
        due.addAction("Pick a date and time...").triggered.connect(lambda: self._window.edit_card(card))
        clear = due.addAction("No due date")
        clear.setEnabled(bool(card.get("due")))
        clear.triggered.connect(lambda: self._set_due(card, None))
        move = menu.addMenu("Move to")
        for col in board.columns():
            action = move.addAction(col["name"])
            action.setEnabled(col["id"] != self.column["id"])
            action.triggered.connect(lambda _c=False, cid=col["id"]: self._move_card(card, cid))
        menu.addSeparator()
        menu.addAction("Delete card").triggered.connect(lambda: self._delete_card(card))
        menu.exec(self.cards.viewport().mapToGlobal(pos))

    def _set_due(self, card: dict, due: str | None) -> None:
        board.update_card(card["id"], due=due)
        self.changed.emit()

    def _move_card(self, card: dict, column_id: str) -> None:
        board.move_card(card["id"], column_id)
        self.changed.emit()

    def _delete_card(self, card: dict) -> None:
        board.delete_card(card["id"])
        self.changed.emit()


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------

class BoardWindow(QDialog):
    def __init__(self, controller=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Board")
        self.setWindowFlag(Qt.WindowMaximizeButtonHint, True)
        self.resize(1040, 640)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)
        head = QHBoxLayout()
        self.heading = QLabel("Board")
        self.hint = QLabel("Drag cards between columns · double-click to edit · Delete removes the "
                           "selected card · or say "
                           "\"add pay rent to to do due friday\"")
        self.hint.setObjectName("muted")
        head.addWidget(self.heading)
        head.addSpacing(12)
        head.addWidget(self.hint, 1)
        self.sideways = QCheckBox("Scroll sideways")
        self.sideways.setToolTip("The mouse wheel moves across the board, even over a column. Hold Shift to "
                                 "scroll a column's cards instead.")
        self.sideways.setChecked(backend.cfg_bool("board_scroll_sideways"))
        self.sideways.toggled.connect(lambda on: backend.persist_keys({"board_scroll_sideways": bool(on)}))
        head.addWidget(self.sideways)
        layout.addLayout(head)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.row_widget = QWidget()
        self.row_widget.setObjectName("boardRow")
        self.row = QHBoxLayout(self.row_widget)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setSpacing(10)
        self.scroll.setWidget(self.row_widget)
        self.scroll.viewport().installEventFilter(self)
        self.row_widget.setAutoFillBackground(False)    # setWidget turns it on (see the Qt notes)
        layout.addWidget(self.scroll, 1)

        self.columns: list[ColumnWidget] = []
        self._pending = False
        if controller is not None:
            controller.board_changed.connect(self._schedule_reload)
        self._day = QTimer(self)                        # "tomorrow" becomes "today" at midnight
        self._day.setInterval(10 * 60 * 1000)
        self._day.timeout.connect(self.reload)
        self._day.start()
        theme_signals.changed.connect(self.restyle)
        self.restyle()
        self.reload()
        frame.install(self, minimize=True, maximize=True)   # NEON's title bar, or the native one (Settings > Appearance)

    def restyle(self) -> None:
        c = COLORS
        enable_dark_title_bar(self)
        self.heading.setStyleSheet(f"color: {c['accent']}; font-size: 20px; font-weight: 800;")
        self.setStyleSheet(
            f"QFrame#column {{ background: {c['panel']}; border: 1px solid {c['border']}; border-radius: 12px; }}"
            f"QLabel#columnName {{ color: {c['text']}; font-weight: 700; font-size: 14px; }}"
            f"QLabel#muted {{ color: {c['muted']}; }}"
            f"QWidget#boardRow {{ background: transparent; }}"
            f"QListWidget {{ background: transparent; border: none; outline: 0; }}"
            f"QListWidget::item {{ background: transparent; border: none; }}"
            f"QListWidget::item:selected {{ background: transparent; }}"
            f"QPushButton#addCard {{ text-align: left; background: transparent; border: none; color: {c['muted']}; "
            f"padding: 6px 8px; border-radius: 8px; }}"
            f"QPushButton#addCard:hover {{ background: {c['panel_alt']}; color: {c['text']}; }}"
            f"QPushButton#addColumn {{ background: {c['panel']}; border: 1px dashed {c['border']}; "
            f"border-radius: 12px; color: {c['muted']}; padding: 10px; text-align: left; }}"
            f"QPushButton#addColumn:hover {{ color: {c['text']}; border-color: {c['accent']}; }}"
            f"QLineEdit {{ background: {c['panel_alt']}; border: 1px solid {c['border']}; border-radius: 8px; "
            f"padding: 6px 8px; }}"
            f"QLineEdit:focus {{ border-color: {c['accent']}; }}"
            f"QToolButton {{ color: {c['muted']}; border: none; padding: 0 4px; font-size: 16px; }}"
            f"QToolButton:hover {{ color: {c['text']}; }}"
            f"QToolButton::menu-indicator {{ image: none; }}"
            f"QScrollBar:horizontal {{ background: {c['bg']}; height: 10px; margin: 0; border: none; }}"
            f"QScrollBar::handle:horizontal {{ background: {c['border']}; border-radius: 5px; min-width: 40px; }}"
            f"QScrollBar::handle:horizontal:hover {{ background: {c['accent_dim']}; }}"
            f"QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}"
            f"QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background: none; }}"
            f"QScrollBar:vertical {{ background: transparent; width: 8px; border: none; }}"
            f"QScrollBar::handle:vertical {{ background: {c['border']}; border-radius: 4px; min-height: 30px; }}"
            f"QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}"
            f"QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}")
        self.reload()

    def _schedule_reload(self) -> None:
        """Voice commands can change the board many times a second; redraw once."""
        if not self._pending:
            self._pending = True
            QTimer.singleShot(30, self._reload_pending)

    def _reload_pending(self) -> None:
        self._pending = False
        self.reload()

    def reload(self, keep_adder: str | None = None) -> None:
        if keep_adder is None:
            keep_adder = next((w.column["id"] for w in self.columns if not w.adder.isHidden()), None)
        scroll = self.scroll.horizontalScrollBar().value()
        while self.row.count():
            item = self.row.takeAt(0)
            widgets = [item.widget()] if item.widget() is not None else []
            if item.layout() is not None:                # the "+ Add another column" stack
                inner = item.layout()
                while inner.count():
                    child = inner.takeAt(0)
                    if child.widget() is not None:
                        widgets.append(child.widget())
            for widget in widgets:
                widget.hide()                            # gone now, not when Qt gets round to deleting it
                widget.deleteLater()
        cols = board.columns()
        self.columns = []
        for i, col in enumerate(cols):
            widget = ColumnWidget(col, i, len(cols), self)
            widget.changed.connect(self._schedule_reload)
            widget.cards.picked.connect(lambda w=widget: self._only_selected(w))
            self.row.addWidget(widget, 0, Qt.AlignTop)
            widget.cards.viewport().installEventFilter(self)
            self.columns.append(widget)
        # "+ Add another column", Trello-style: a button that becomes a name box.
        self.add_column_btn = QPushButton("+  Add another column")
        self.add_column_btn.setObjectName("addColumn")
        self.add_column_btn.setFixedWidth(COLUMN_WIDTH)
        self.add_column_btn.clicked.connect(self._open_column_adder)
        self.column_name = QLineEdit()
        self.column_name.setPlaceholderText("Column name, then Enter")
        self.column_name.setMaxLength(board.COLUMN_MAX)
        self.column_name.setFixedWidth(COLUMN_WIDTH)
        self.column_name.setAccessibleName("New column name")
        self.column_name.returnPressed.connect(self._add_column)
        self.column_name.editingFinished.connect(lambda: QTimer.singleShot(0, self._close_column_adder))
        self.column_name.hide()
        tail = QVBoxLayout()
        tail.addWidget(self.add_column_btn)
        tail.addWidget(self.column_name)
        tail.addStretch(1)
        self.row.addLayout(tail)
        self.row.addStretch(1)
        self.scroll.horizontalScrollBar().setValue(scroll)
        for widget in self.columns:
            if widget.column["id"] == keep_adder:
                widget.open_adder()

    # ---- the mouse wheel ---------------------------------------------------------------------------
    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Type.Wheel:
            return self.wheel(obj, event)
        return super().eventFilter(obj, event)

    def wheel(self, obj, event) -> bool:
        """Over a column: its cards scroll (smoothly), unless "Scroll sideways" is on. Shift swaps the two.
        Over the empty board, or over a column with nothing to scroll: across the board."""
        cards = next((w.cards for w in self.columns if w.cards.viewport() is obj), None)
        shift = bool(event.modifiers() & Qt.ShiftModifier)
        sideways = self.sideways.isChecked() != shift
        if cards is not None and not sideways and cards.verticalScrollBar().maximum() > 0:
            bar, step = cards.verticalScrollBar(), WHEEL_STEP_CARDS
        else:
            bar, step = self.scroll.horizontalScrollBar(), WHEEL_STEP_SIDEWAYS
        pixel = event.pixelDelta()
        if not pixel.isNull():                               # a touchpad: already smooth, follow it exactly
            amount = pixel.x() if pixel.x() and bar.orientation() == Qt.Horizontal else pixel.y() or pixel.x()
            SmoothScroll.of(bar).by(-amount, animate=False)
            return True
        angle = event.angleDelta()
        notches = (angle.y() or angle.x()) / 120.0           # Windows turns Shift+wheel into a sideways delta
        if notches:
            SmoothScroll.of(bar).by(-notches * step)
        return True

    def _only_selected(self, keep: "ColumnWidget") -> None:
        """One selected card on the whole board, like Trello: picking one clears the other columns."""
        for widget in self.columns:
            if widget is not keep:
                widget.cards.deselect()

    def _open_column_adder(self) -> None:
        self.add_column_btn.hide()
        self.column_name.show()
        self.column_name.setFocus()

    def _close_column_adder(self) -> None:
        try:
            if self.column_name.hasFocus():
                return
            self.column_name.clear()
            self.column_name.hide()
            self.add_column_btn.show()
        except RuntimeError:
            pass                                        # redrawn meanwhile

    def _add_column(self) -> None:
        name = self.column_name.text().strip()
        if not name:
            return
        board.add_column(name)
        self.reload()
        QTimer.singleShot(0, lambda: self.scroll.horizontalScrollBar().setValue(
            self.scroll.horizontalScrollBar().maximum()))

    def edit_card(self, card: dict) -> None:
        dialog = CardDialog(card, self)
        if dialog.exec():
            self.reload()

    def showEvent(self, event) -> None:
        self.reload()
        super().showEvent(event)
