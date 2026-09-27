/*
    NEON Assistant on the Plasma panel: the status bar's orb and caption in the panel, and its recent lines and
    buttons in a pop-up. It follows NEON through the panel feed (panel_feed.py): a long poll on
    http://127.0.0.1:<port>/status, and POST /command for the buttons.
*/
import QtQuick
import QtQuick.Layouts
import org.kde.plasma.plasmoid
import org.kde.plasma.core as PlasmaCore
import org.kde.plasma.components as PlasmaComponents
import org.kde.plasma.plasma5support as P5Support
import org.kde.kirigami as Kirigami

PlasmoidItem {
    id: root

    property var feed: ({})
    property bool online: false
    property int seq: -1
    property int generation: 0             // bumped when the port changes: older polls stop
    property string shownSender: ""
    property string shownText: ""

    readonly property string base: "http://127.0.0.1:" + Plasmoid.configuration.port
    readonly property string neonName: online && feed.name ? feed.name : "NEON"
    readonly property string neonState: {
        if (!online)
            return "offline"
        var state = feed.state || "idle"
        return feed.muted && state === "idle" ? "muted" : state
    }
    readonly property string captionLine: {
        if (shownText)
            return (shownSender && shownSender !== neonName && shownSender !== "You" ? shownSender + ": " : "")
                   + shownText
        return online ? stateLabel(neonState) : i18n("NEON isn't running")
    }
    readonly property int pulseMs: neonState === "listening" ? 700 : neonState === "thinking" ? 450 : 0
    readonly property bool canLaunch: {
        var command = String(Plasmoid.configuration.launchCommand || "")
        return command !== "" && command.indexOf("@LAUNCH@") < 0
    }

    Plasmoid.icon: "audio-input-microphone"
    preferredRepresentation: compactRepresentation
    toolTipMainText: neonName
    toolTipSubText: online ? stateLabel(neonState) + (shownText ? "\n" + shownText : "") : i18n("NEON isn't running")

    // ---- talking to NEON ------------------------------------------------------------------------------------
    function stateLabel(state) {
        switch (state) {
        case "listening": return i18n("Listening")
        case "thinking": return i18n("Thinking")
        case "speaking": return i18n("Speaking")
        case "error": return i18n("Something went wrong")
        case "muted": return i18n("Microphone muted")
        case "offline": return i18n("Not running")
        default: return feed.wake ? i18n("Say \"%1\"", feed.wake_phrase || "hey nova") : i18n("Ready")
        }
    }

    function stateColor(state) {
        var colors = feed.colors || {}
        var key = state === "offline" ? "" : state
        if (Plasmoid.configuration.useNeonColors && key && colors[key])
            return colors[key]
        switch (state) {
        case "listening": return Kirigami.Theme.positiveTextColor
        case "thinking": return Kirigami.Theme.neutralTextColor
        case "speaking": return Kirigami.Theme.highlightColor
        case "error":
        case "muted": return Kirigami.Theme.negativeTextColor
        case "offline": return Kirigami.Theme.disabledTextColor
        default: return Kirigami.Theme.textColor
        }
    }

    function apply(data) {
        var fresh = !online
        online = true
        if (data.text && (fresh || data.text !== feed.text || data.sender !== feed.sender)) {
            shownSender = data.sender || ""
            shownText = data.text
            captionTimer.restart()
        }
        feed = data
        seq = data.seq
        if (data.state && data.state !== "idle")
            captionTimer.restart()             // keep the line while NEON is still busy with it
    }

    function poll() {
        var mine = generation
        var xhr = new XMLHttpRequest()
        xhr.onreadystatechange = function() {
            if (xhr.readyState !== XMLHttpRequest.DONE || mine !== generation)
                return
            var data = null
            if (xhr.status === 200) {
                try { data = JSON.parse(xhr.responseText) } catch (e) { data = null }
            }
            if (data) {
                apply(data)
                Qt.callLater(poll)
            } else {
                online = false
                seq = -1
                retryTimer.start()
            }
        }
        xhr.open("GET", base + "/status" + (online && seq >= 0 ? "?since=" + seq : ""))
        xhr.send()
    }

    function restartPolling() {
        generation += 1
        online = false
        seq = -1
        retryTimer.stop()
        poll()
    }

    function send(command) {
        if (!online) {
            if ((command === "talk" || command === "show") && canLaunch)
                launcher.connectSource(Plasmoid.configuration.launchCommand)
            return
        }
        var xhr = new XMLHttpRequest()
        xhr.open("POST", base + "/command")
        xhr.setRequestHeader("X-Neon-Widget", "1")
        xhr.setRequestHeader("Content-Type", "text/plain")
        xhr.send(command)
    }

    function escaped(text) {
        return String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    }

    Timer {
        id: retryTimer
        interval: 3000
        onTriggered: root.poll()
    }

    Timer {
        id: captionTimer
        interval: Math.max(1, Plasmoid.configuration.captionSeconds) * 1000
        onTriggered: {
            if (root.feed.state && root.feed.state !== "idle")
                restart()
            else {
                root.shownText = ""
                root.shownSender = ""
            }
        }
    }

    P5Support.DataSource {
        id: launcher
        objectName: "launcher"
        engine: "executable"
        connectedSources: []
        onNewData: (source, data) => disconnectSource(source)
    }

    Connections {
        target: Plasmoid.configuration
        function onPortChanged() { root.restartPolling() }
    }

    Component.onCompleted: poll()

    // ---- the orb (an inline component can't see this file's ids: everything comes in through its properties) --
    component Orb: Item {
        id: orb
        property real size: Kirigami.Units.iconSizes.small
        property color tint: Kirigami.Theme.textColor
        property real level: 0                              // NEON's voice, while it speaks
        property bool lit: true                             // NEON is running
        property int pulseMs: 0                             // breathing speed; 0 = still
        implicitWidth: size
        implicitHeight: size

        Rectangle {                                         // the glow
            anchors.centerIn: parent
            width: orb.size
            height: width
            radius: width / 2
            color: orb.tint
            opacity: orb.lit ? 0.28 : 0.12
            scale: 0.75 + orb.level * 0.5 + breath.extra
            Behavior on scale { NumberAnimation { duration: 90 } }
        }
        Rectangle {                                         // the dot
            anchors.centerIn: parent
            width: orb.size * 0.55
            height: width
            radius: width / 2
            color: orb.tint
            opacity: orb.lit ? 1 : 0.5
            Behavior on color { ColorAnimation { duration: 200 } }
        }

        QtObject {
            id: breath
            property real extra: 0
        }
        SequentialAnimation {
            running: orb.pulseMs > 0
            loops: Animation.Infinite
            NumberAnimation { target: breath; property: "extra"; from: 0; to: 0.25; duration: Math.max(1, orb.pulseMs); easing.type: Easing.InOutSine }
            NumberAnimation { target: breath; property: "extra"; from: 0.25; to: 0; duration: Math.max(1, orb.pulseMs); easing.type: Easing.InOutSine }
            onStopped: breath.extra = 0
        }
    }

    // ---- in the panel ---------------------------------------------------------------------------------------
    compactRepresentation: MouseArea {
        id: compact
        objectName: "compact"
        readonly property bool vertical: Plasmoid.formFactor === PlasmaCore.Types.Vertical
        readonly property bool withText: Plasmoid.configuration.showCaption && !vertical
        acceptedButtons: Qt.LeftButton | Qt.MiddleButton
        hoverEnabled: true

        Layout.minimumWidth: withText ? Kirigami.Units.gridUnit * Plasmoid.configuration.captionWidth : -1
        Layout.preferredWidth: withText ? Kirigami.Units.gridUnit * Plasmoid.configuration.captionWidth : -1

        onClicked: (mouse) => {
            if (mouse.button === Qt.MiddleButton)
                root.send("mute")
            else if (Plasmoid.configuration.clickAction === "popup" || !root.online && !root.canLaunch)
                root.expanded = !root.expanded
            else
                root.send("talk")
        }

        RowLayout {
            anchors.fill: parent
            spacing: Kirigami.Units.smallSpacing

            Orb {
                objectName: "panelOrb"
                size: Math.min(compact.height, compact.vertical ? compact.width : compact.height)
                tint: root.stateColor(root.neonState)
                level: root.neonState === "speaking" ? (root.feed.level || 0) : 0
                lit: root.online
                pulseMs: root.pulseMs
                Layout.alignment: Qt.AlignCenter
                Layout.fillWidth: !compact.withText
            }
            PlasmaComponents.Label {
                objectName: "captionLabel"
                visible: compact.withText
                Layout.fillWidth: true
                text: root.captionLine
                elide: Text.ElideRight
                maximumLineCount: 1
                opacity: root.shownText ? 1 : 0.6
            }
        }
    }

    // ---- the pop-up -----------------------------------------------------------------------------------------
    fullRepresentation: ColumnLayout {
        objectName: "full"
        Layout.preferredWidth: Kirigami.Units.gridUnit * 22
        Layout.preferredHeight: Kirigami.Units.gridUnit * 18
        spacing: Kirigami.Units.smallSpacing

        RowLayout {
            spacing: Kirigami.Units.largeSpacing
            Orb {
                size: Kirigami.Units.iconSizes.medium
                tint: root.stateColor(root.neonState)
                level: root.neonState === "speaking" ? (root.feed.level || 0) : 0
                lit: root.online
                pulseMs: root.pulseMs
            }
            ColumnLayout {
                spacing: 0
                Kirigami.Heading { level: 3; text: root.neonName }
                PlasmaComponents.Label { text: root.stateLabel(root.neonState); opacity: 0.7 }
            }
        }

        PlasmaComponents.ScrollView {
            Layout.fillWidth: true
            Layout.fillHeight: true
            visible: root.online
            ListView {
                objectName: "history"
                model: root.feed.history || []
                spacing: Kirigami.Units.smallSpacing
                clip: true
                delegate: PlasmaComponents.Label {
                    required property var modelData
                    width: ListView.view ? ListView.view.width : implicitWidth
                    wrapMode: Text.Wrap
                    textFormat: Text.StyledText
                    text: "<b>" + root.escaped(modelData.sender) + "</b>  " + root.escaped(modelData.text)
                }
                onCountChanged: positionViewAtEnd()
            }
        }

        ColumnLayout {
            visible: !root.online
            Layout.fillWidth: true
            Layout.fillHeight: true
            PlasmaComponents.Label {
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                text: root.canLaunch ? i18n("NEON isn't running.")
                                     : i18n("NEON isn't running. Start it, then this widget follows it by itself.")
            }
            PlasmaComponents.Button {
                visible: root.canLaunch
                icon.name: "media-playback-start"
                text: i18n("Start NEON")
                onClicked: launcher.connectSource(Plasmoid.configuration.launchCommand)
            }
        }

        GridLayout {
            objectName: "buttons"
            visible: root.online
            columns: 4
            Layout.fillWidth: true
            PlasmaComponents.ToolButton {
                icon.name: "audio-input-microphone"; text: i18n("Talk")
                onClicked: root.send("talk")
            }
            PlasmaComponents.ToolButton {
                icon.name: "edit-find"; text: i18n("Type")
                onClicked: { root.expanded = false; root.send("quick") }
            }
            PlasmaComponents.ToolButton {
                icon.name: "media-playback-stop"; text: i18n("Stop")
                enabled: root.neonState === "speaking"
                onClicked: root.send("stop")
            }
            PlasmaComponents.ToolButton {
                icon.name: "window"; text: i18n("Open")
                onClicked: { root.expanded = false; root.send("show") }
            }
            PlasmaComponents.ToolButton {
                icon.name: "microphone-sensitivity-muted"; text: i18n("Mute")
                checkable: true
                checked: !!root.feed.muted
                onClicked: { checked = Qt.binding(() => !!root.feed.muted); root.send("mute") }
            }
            PlasmaComponents.ToolButton {
                icon.name: "audio-ready"; text: i18n("Wake word")
                checkable: true
                checked: !!root.feed.wake
                onClicked: { checked = Qt.binding(() => !!root.feed.wake); root.send("wake") }
            }
            PlasmaComponents.ToolButton {
                icon.name: "edit-entry"; text: i18n("Dictation")
                checkable: true
                checked: !!root.feed.dictating
                onClicked: { checked = Qt.binding(() => !!root.feed.dictating); root.send("dictation") }
            }
            PlasmaComponents.ToolButton {
                icon.name: "configure"; text: i18n("Settings")
                onClicked: { root.expanded = false; root.send("settings") }
            }
        }
    }

    // ---- right-click ----------------------------------------------------------------------------------------
    Plasmoid.contextualActions: [
        PlasmaCore.Action {
            text: i18n("Talk")
            icon.name: "audio-input-microphone"
            enabled: root.online
            onTriggered: root.send("talk")
        },
        PlasmaCore.Action {
            text: i18n("Type a command…")
            icon.name: "edit-find"
            enabled: root.online
            onTriggered: root.send("quick")
        },
        PlasmaCore.Action {
            text: i18n("Mute the microphone")
            icon.name: "microphone-sensitivity-muted"
            checkable: true
            checked: !!root.feed.muted
            enabled: root.online
            onTriggered: root.send("mute")
        },
        PlasmaCore.Action {
            text: i18n("Listen for the wake word")
            icon.name: "audio-ready"
            checkable: true
            checked: !!root.feed.wake
            enabled: root.online
            onTriggered: root.send("wake")
        },
        PlasmaCore.Action {
            text: i18n("Show recent lines")
            icon.name: "view-list-text"
            onTriggered: root.expanded = true
        },
        PlasmaCore.Action {
            text: root.online ? i18n("Open NEON") : i18n("Start NEON")
            icon.name: "window"
            enabled: root.online || root.canLaunch
            onTriggered: root.send("show")
        }
    ]
}
