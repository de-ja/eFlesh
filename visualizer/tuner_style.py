"""Colors and pens shared by the tuner's tabs."""

from PySide6 import QtCore, QtGui
import pyqtgraph as pg

# One color per chip position (middle, left, right, top, bottom), so a position
# keeps its color whichever chips are shown; board 1 is drawn dashed. These are
# the first five slots of a categorical palette checked for colour-blind
# separation and >= 3:1 contrast on PLOT_BG.
CHIP_COLORS = ("#3987e5", "#d95926", "#199e70", "#c98500", "#d55181")
PLOT_BG = "#1a1a19"
PLOT_FG = "#c3c2b7"
LINE_WIDTH = 2


def chip_pen(k):
    board, i = divmod(k, len(CHIP_COLORS))
    style = QtCore.Qt.DashLine if board else QtCore.Qt.SolidLine
    return pg.mkPen(CHIP_COLORS[i], width=LINE_WIDTH, style=style)


def chip_icon(k):
    pm = QtGui.QPixmap(22, 10)
    pm.fill(QtCore.Qt.transparent)
    painter = QtGui.QPainter(pm)
    painter.setRenderHint(QtGui.QPainter.Antialiasing)
    painter.setPen(chip_pen(k))
    painter.drawLine(1, 5, 21, 5)
    painter.end()
    return QtGui.QIcon(pm)
