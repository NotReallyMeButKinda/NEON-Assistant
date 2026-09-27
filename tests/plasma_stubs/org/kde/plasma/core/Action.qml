import QtQuick

// PlasmaCore.Action: a QAction for the widget's right-click menu. trigger() flips `checked` the way QAction
// does (from C++, so a QML binding on it survives).
QtObject {
    id: action
    property string text
    property bool checkable: false
    property bool checked: false
    property bool enabled: true
    property ActionIcon icon: ActionIcon {}
    signal triggered()
    function trigger() {
        if (!enabled)
            return
        triggered()
    }
}
