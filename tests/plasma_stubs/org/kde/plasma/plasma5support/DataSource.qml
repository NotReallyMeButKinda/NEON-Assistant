import QtQuick

// Records the commands the widget would run (the executable engine runs each source as a shell command).
QtObject {
    property string engine
    property var connectedSources: []
    property var ran: []
    signal newData(string source, var data)
    function connectSource(source) {
        connectedSources = connectedSources.concat([source])
        ran = ran.concat([source])
    }
    function disconnectSource(source) {
        connectedSources = connectedSources.filter(s => s !== source)
    }
}
