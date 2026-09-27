"""Load the Plasma widget (plasmoid/org.neon.assistant) in plain Qt, with stand-ins for Plasma's QML modules.

Plasma isn't installed here (and doesn't run on Windows), so this registers just enough of it for the widget's
own QML to run for real: PlasmoidItem and the attached `Plasmoid` (org.kde.plasma.plasmoid) from Python, and
PlasmaCore / Kirigami / Plasma Components / plasma5support as small QML modules in tests/plasma_stubs/. i18n() comes from the root context, as KLocalizedContext provides it in Plasma.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Property, QObject, QUrl, Signal, Slot
from PySide6.QtQml import (ListProperty, QmlAttached, QQmlComponent, QQmlEngine, QQmlPropertyMap, qmlRegisterType,
                           qmlRegisterUncreatableType)
from PySide6.QtQuick import QQuickItem

ROOT = Path(__file__).resolve().parent.parent
WIDGET = ROOT / "plasmoid" / "org.neon.assistant"
STUBS = Path(__file__).resolve().parent / "plasma_stubs"

CONFIG = QQmlPropertyMap()                            # every widget's Plasmoid.configuration
FORM_FACTOR = {"value": 2}                            # Types.Horizontal: a horizontal panel


# ---- org.kde.plasma.plasmoid ------------------------------------------------------------------------------

class PlasmoidAttached(QObject):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._actions: list = []
        self._icon = ""

    def _append(self, action):
        self._actions.append(action)

    def _count(self):
        return len(self._actions)

    def _at(self, index):
        return self._actions[index]

    def _clear(self):
        self._actions.clear()

    contextualActions = ListProperty(QObject, append=_append, count=_count, at=_at, clear=_clear)
    configuration = Property(QObject, lambda self: CONFIG, constant=True)
    formFactor = Property(int, lambda self: FORM_FACTOR["value"], constant=True)

    def _set_icon(self, value):
        self._icon = value
        self.changed.emit()

    icon = Property(str, lambda self: self._icon, _set_icon, notify=changed)

    def actions(self) -> list:
        return list(self._actions)


_ATTACHED: dict[int, PlasmoidAttached] = {}


@QmlAttached(PlasmoidAttached)
class PlasmoidItem(QQuickItem):
    """The widget's root: its two representations, whether the pop-up is open, and its tooltip."""
    compactChanged = Signal()
    fullChanged = Signal()
    preferredChanged = Signal()
    expandedChanged = Signal()
    tipChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._compact = self._full = self._preferred = None
        self._expanded = False
        self._tip_main = self._tip_sub = ""

    def _value(attr, kind, changed):  # noqa: N805 -- a small property factory
        def get(self):
            return getattr(self, attr)

        def set_(self, value):
            if getattr(self, attr) is not value and getattr(self, attr) != value:
                setattr(self, attr, value)
                getattr(self, changed).emit()
        return get, set_

    compactRepresentation = Property(QQmlComponent, *_value("_compact", QQmlComponent, "compactChanged"),
                                     notify=compactChanged)
    fullRepresentation = Property(QQmlComponent, *_value("_full", QQmlComponent, "fullChanged"), notify=fullChanged)
    preferredRepresentation = Property(QQmlComponent, *_value("_preferred", QQmlComponent, "preferredChanged"),
                                       notify=preferredChanged)
    expanded = Property(bool, *_value("_expanded", bool, "expandedChanged"), notify=expandedChanged)
    toolTipMainText = Property(str, *_value("_tip_main", str, "tipChanged"), notify=tipChanged)
    toolTipSubText = Property(str, *_value("_tip_sub", str, "tipChanged"), notify=tipChanged)

    @staticmethod
    def qmlAttachedProperties(cls, obj):  # noqa: N805 -- PySide's signature
        attached = _ATTACHED.get(id(obj))
        if attached is None:
            attached = _ATTACHED[id(obj)] = PlasmoidAttached(obj)
        return attached


class FormDataAttached(QObject):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._label, self._section = "", False

    def _set_label(self, value):
        self._label = value
        self.changed.emit()

    def _set_section(self, value):
        self._section = value
        self.changed.emit()

    label = Property(str, lambda self: self._label, _set_label, notify=changed)
    isSection = Property(bool, lambda self: self._section, _set_section, notify=changed)


@QmlAttached(FormDataAttached)
class FormData(QObject):
    """Kirigami.FormData, the labels of a FormLayout's rows (an attached property, so it comes from Python)."""

    @staticmethod
    def qmlAttachedProperties(cls, obj):  # noqa: N805
        return FormDataAttached(obj)


class _Locale(QObject):
    """i18n() for the widget, as Plasma's KLocalizedContext gives it: %1, %2... filled in."""

    @Slot(str, result=str)
    @Slot(str, "QVariant", result=str)
    @Slot(str, "QVariant", "QVariant", result=str)
    def i18n(self, text, *args):
        for n, value in enumerate(args, 1):
            text = text.replace(f"%{n}", str(value))
        return text


_REGISTERED = {"done": False}


def register() -> None:
    if _REGISTERED["done"]:
        return
    _REGISTERED["done"] = True
    qmlRegisterType(PlasmoidItem, "org.kde.plasma.plasmoid", 2, 0, "PlasmoidItem")
    qmlRegisterUncreatableType(PlasmoidItem, "org.kde.plasma.plasmoid", 2, 0, "Plasmoid",
                               "Plasmoid is an attached property")
    qmlRegisterUncreatableType(FormData, "org.kde.kirigami", 2, 0, "FormData", "FormData is an attached property")


def configure(**values) -> None:
    for key, value in values.items():
        CONFIG.insert(key, value)


def default_configuration() -> dict:
    """The widget's defaults, from its contents/config/main.xml."""
    import xml.etree.ElementTree as ET
    ns = {"k": "http://www.kde.org/standards/kcfg/1.0"}
    out = {}
    for entry in ET.parse(WIDGET / "contents" / "config" / "main.xml").getroot().iterfind(".//k:entry", ns):
        default = entry.findtext("k:default", "", ns)
        kind = entry.get("type")
        out[entry.get("name")] = (default == "true") if kind == "Bool" else int(default) if kind == "Int" else default
    return out


class Widget:
    """The loaded widget: `root` (the PlasmoidItem), and its representations once built."""

    def __init__(self, **config):
        register()
        configure(**dict(default_configuration(), **config))
        self.engine = QQmlEngine()
        self.engine.addImportPath(str(STUBS))
        self._locale = _Locale()
        self.engine.rootContext().setContextObject(self._locale)
        self.errors: list[str] = []
        self.engine.warnings.connect(lambda warnings: self.errors.extend(w.toString() for w in warnings))
        component = QQmlComponent(self.engine, QUrl.fromLocalFile(str(WIDGET / "contents" / "ui" / "main.qml")))
        self.root = component.create()
        if self.root is None:
            raise RuntimeError("the widget didn't load:\n" + "\n".join(e.toString() for e in component.errors()))
        self.compact = self._build(self.root.property("compactRepresentation"))
        self.full = self._build(self.root.property("fullRepresentation"))

    def _build(self, component):
        item = component.create(QQmlEngine.contextForObject(self.root))   # as Plasma does: `root` etc. in scope
        if item is None:
            raise RuntimeError("\n".join(e.toString() for e in component.errors()))
        item.setParentItem(self.root)
        return item

    def attached(self) -> PlasmoidAttached:
        return _ATTACHED[id(self.root)]

    def find(self, item, object_name: str):
        return item.findChild(QObject, object_name)

    def close(self) -> None:
        self.root.setProperty("generation", self.root.property("generation") + 1)   # stop its polling
        for item in (self.compact, self.full, self.root):
            item.deleteLater()


def load_page(relative: str):
    """One of the widget's other QML files (its settings page, its config model), as Plasma would load it."""
    register()
    engine = QQmlEngine()
    engine.addImportPath(str(STUBS))
    locale = _Locale()
    engine.rootContext().setContextObject(locale)
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(WIDGET / relative)))
    obj = component.create()
    if obj is None:
        raise RuntimeError("\n".join(e.toString() for e in component.errors()))
    _KEEP.append((obj, engine, locale, component))     # alive for the test's lifetime
    return obj


_KEEP: list = []
