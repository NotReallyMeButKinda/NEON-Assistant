import QtQuick
import QtQuick.Controls

ScrollView {
    default property alias content: holder.data
    Item { id: holder; width: parent ? parent.width : 400; implicitHeight: childrenRect.height }
}
