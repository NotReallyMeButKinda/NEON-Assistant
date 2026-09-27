import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts
import org.kde.kirigami as Kirigami
import org.kde.kcmutils as KCM

KCM.SimpleKCM {
    id: page

    property string cfg_clickAction
    property alias cfg_showCaption: showCaption.checked
    property alias cfg_captionWidth: captionWidth.value
    property alias cfg_captionSeconds: captionSeconds.value
    property alias cfg_useNeonColors: useNeonColors.checked
    property alias cfg_port: port.value
    property alias cfg_launchCommand: launchCommand.text

    Kirigami.FormLayout {
        QQC2.ComboBox {
            id: clickAction
            Kirigami.FormData.label: i18n("Clicking the widget:")
            textRole: "text"
            valueRole: "value"
            model: [
                { text: i18n("Starts listening"), value: "talk" },
                { text: i18n("Opens its panel"), value: "popup" }
            ]
            currentIndex: indexOfValue(page.cfg_clickAction)
            onActivated: page.cfg_clickAction = currentValue
        }

        QQC2.Label {
            text: i18n("Middle-click mutes the microphone. Right-click has everything else.")
            font: Kirigami.Theme.smallFont
            opacity: 0.7
        }

        Item { Kirigami.FormData.isSection: true }

        QQC2.CheckBox {
            id: showCaption
            Kirigami.FormData.label: i18n("On the panel:")
            text: i18n("Show what NEON hears and says")
        }

        QQC2.SpinBox {
            id: captionWidth
            Kirigami.FormData.label: i18n("Text width:")
            enabled: showCaption.checked
            from: 6
            to: 60
        }

        QQC2.SpinBox {
            id: captionSeconds
            Kirigami.FormData.label: i18n("Keep a line for (seconds):")
            enabled: showCaption.checked
            from: 1
            to: 120
        }

        QQC2.CheckBox {
            id: useNeonColors
            text: i18n("Colour the orb with NEON's theme")
        }

        Item { Kirigami.FormData.isSection: true }

        QQC2.SpinBox {
            id: port
            Kirigami.FormData.label: i18n("NEON's port:")
            from: 1024
            to: 65535
            textFromValue: (value) => String(value)
            valueFromText: (text) => parseInt(text)
        }

        QQC2.TextField {
            id: launchCommand
            Kirigami.FormData.label: i18n("Start NEON with:")
            Layout.fillWidth: true
            placeholderText: i18n("the command that starts NEON")
        }

        QQC2.Label {
            Layout.fillWidth: true
            wrapMode: Text.Wrap
            text: i18n("The port must match NEON's Settings > Status bar > Panel widget. The command is used by the widget's \"Start NEON\" button.")
            font: Kirigami.Theme.smallFont
            opacity: 0.7
        }
    }
}
