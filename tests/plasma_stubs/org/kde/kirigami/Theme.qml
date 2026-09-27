pragma Singleton
import QtQuick

QtObject {
    readonly property color textColor: "#eff0f1"
    readonly property color disabledTextColor: "#6e7175"
    readonly property color highlightColor: "#3daee9"
    readonly property color positiveTextColor: "#27ae60"
    readonly property color neutralTextColor: "#f67400"
    readonly property color negativeTextColor: "#da4453"
    readonly property color backgroundColor: "#232629"
    readonly property font smallFont: Qt.font({ pointSize: 8 })
}
